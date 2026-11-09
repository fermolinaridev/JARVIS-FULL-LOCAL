"""
JARVIS — engine de TTS local com clonagem de voz via Coqui XTTS-v2.

Estratégia (ordem de preferência por fala):
  1. CLONE: se existir um .wav em samples/ (canônico: jarvis_voice.wav),
     usa ele como timbre de referência (clonagem zero-shot).
  2. BUILT-IN: senão, usa um speaker que vem de fábrica no XTTS-v2
     (default "Damien Black" — masculino grave). Override por
     JARVIS_TTS_SPEAKER (ex.: "Viktor Eka", "Royston Min").
  3. ERRO: se também não tiver torch/TTS, o /api/tts devolve 503 e o
     frontend cai pra Web Speech API.

Modelo:
  tts_models/multilingual/multi-dataset/xtts_v2
  - Multilíngue (en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar,
    zh-cn, hu, ko, ja, hi)
  - 30 speakers built-in (16 femininos, 14 masculinos)
  - Clonagem zero-shot a partir de ~6s de áudio de referência
  - Inferência ~1x realtime em GPU mid-range (RTX 5060), ~5-8x em CPU
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.tts")

# Aceita o ToS do Coqui automaticamente (evita prompt interativo)
os.environ.setdefault("COQUI_TOS_AGREED", "1")


# Caminhos / constantes
ROOT = Path(__file__).resolve().parent
SAMPLES_DIR = ROOT / "samples"
DEFAULT_SAMPLE_NAME = "jarvis_voice.wav"
MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"

# Speaker built-in default — masculino grave, sotaque britânico levinho,
# é o mais "JARVIS-ish" dos 30 disponíveis no XTTS-v2.
DEFAULT_SPEAKER = os.environ.get("JARVIS_TTS_SPEAKER", "Damien Black")

# Vozes masculinas conhecidas no XTTS-v2 (pra `/api/tts/voices`)
MALE_SPEAKERS = (
    "Damien Black", "Viktor Eka", "Royston Min", "Baldur Sanjin",
    "Craig Gutsy", "Andrew Chipper", "Ludvig Milivoj", "Abrahan Mack",
    "Adde Michal", "Badr Odhiambo", "Dionisio Schuyler", "Gilberto Mathias",
    "Ilkin Urbano", "Kazuhiko Atallah", "Suad Qasim",
)


def _clean_for_tts(text: str) -> str:
    """
    O XTTS-v2 em PT costuma LER o ponto final em voz alta ("graus ponto").
    Troca ponto de fim de frase por vírgula e tira o do final (testado:
    "ponto" falado em 4/9 falas -> 0/9). Não mexe em decimais ("3.5"),
    só em pontos seguidos de espaço ou fim do texto.
    """
    return re.sub(r"\.+(\s+|$)", lambda m: "," + m.group(1) if m.group(1) else "", text).strip()


def _resolve_sample() -> Optional[Path]:
    """
    Resolve a amostra de voz pra clonagem.
    Procura:
      1. samples/jarvis_voice.wav (canônico)
      2. Qualquer .wav em samples/ (primeiro alfabético)
    """
    canonical = SAMPLES_DIR / DEFAULT_SAMPLE_NAME
    if canonical.is_file():
        return canonical
    if SAMPLES_DIR.is_dir():
        wavs = sorted(SAMPLES_DIR.glob("*.wav"))
        if wavs:
            return wavs[0]
    return None


class TTSEngine:
    """Wrapper preguiçoso em volta do Coqui XTTS-v2."""

    def __init__(self) -> None:
        self._tts = None              # instância da classe TTS do Coqui
        self._device = "cpu"
        self._lock = threading.Lock() # XTTS não é thread-safe
        self._load_error: Optional[str] = None
        self._loaded = False
        self._sample_path: Optional[Path] = None
        self._speaker: str = DEFAULT_SPEAKER
        self._available_speakers: list[str] = []

    @property
    def available(self) -> bool:
        """True se ainda dá pra tentar carregar (não falhou definitivamente)."""
        return self._load_error is None

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def status(self) -> dict:
        sample = _resolve_sample()
        return {
            "loaded": self._loaded,
            "device": self._device,
            "error": self._load_error,
            "sample": str(sample) if sample else None,
            "mode": "clone" if sample else ("builtin" if self._speaker else "none"),
            "speaker": self._speaker,
            "available_speakers": self._available_speakers,
            "model": MODEL_NAME,
        }

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._load_error:
            raise RuntimeError(self._load_error)

        # imports pesados — só agora
        try:
            import torch
            from TTS.api import TTS  # type: ignore
        except Exception as e:
            self._load_error = f"dependências de TTS indisponíveis: {e}"
            log.warning(self._load_error)
            raise RuntimeError(self._load_error) from e

        # Detecta GPU
        try:
            if torch.cuda.is_available():
                self._device = "cuda"
                log.info("TTS: GPU detectada (%s)", torch.cuda.get_device_name(0))
            else:
                self._device = "cpu"
                log.info("TTS: rodando em CPU (mais lento)")
        except Exception:
            self._device = "cpu"

        # Carrega o modelo (pode demorar 30-60s na 1ª vez por causa do download)
        t0 = time.time()
        log.info("TTS: carregando %s em %s ...", MODEL_NAME, self._device)
        try:
            self._tts = TTS(model_name=MODEL_NAME, progress_bar=False).to(self._device)
        except Exception as e:
            self._load_error = f"falha ao carregar XTTS-v2: {e}"
            log.exception("TTS: falha ao carregar modelo")
            raise RuntimeError(self._load_error) from e

        # Lista de speakers built-in
        try:
            speakers = list(getattr(self._tts, "speakers", []) or [])
            self._available_speakers = speakers
        except Exception:
            self._available_speakers = []

        # Valida o speaker default — se não existir nessa versão do modelo,
        # cai pro primeiro masculino disponível.
        if self._available_speakers and self._speaker not in self._available_speakers:
            fallback = next(
                (s for s in MALE_SPEAKERS if s in self._available_speakers),
                self._available_speakers[0],
            )
            log.warning(
                "TTS: speaker '%s' não existe nesse modelo — usando '%s'",
                self._speaker, fallback,
            )
            self._speaker = fallback

        self._loaded = True
        log.info(
            "TTS: pronto em %.1fs (sample=%s, speaker=%s)",
            time.time() - t0,
            _resolve_sample(),
            self._speaker,
        )

    def synthesize(self, text: str, language: str = "pt") -> bytes:
        """
        Sintetiza `text` em `language` (default pt) e devolve WAV bytes.
        Preferência:
          1. clone da amostra em samples/
          2. speaker built-in (DEFAULT_SPEAKER)
        Levanta RuntimeError se nenhum dos dois for utilizável.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("texto vazio")

        # XTTS divide chunks de até 250 chars internamente; truncamos em
        # 800 (~30s) pra evitar latência absurda numa única request.
        if len(text) > 800:
            text = text[:797] + "..."
        text = _clean_for_tts(text)

        sample = _resolve_sample()
        # Não tem nem sample nem speaker built-in => sem o que cantar.
        if not sample and not self._speaker:
            raise RuntimeError(
                "TTS sem voz: ponha um .wav em samples/jarvis_voice.wav "
                "ou defina JARVIS_TTS_SPEAKER"
            )

        with self._lock:
            self._sample_path = sample
            self._ensure_loaded()

            t0 = time.time()
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                out_path = Path(tmp.name)
            try:
                kwargs = {
                    "text": text,
                    "language": language,
                    "file_path": str(out_path),
                }
                if sample:
                    kwargs["speaker_wav"] = str(sample)
                    mode = "clone"
                else:
                    kwargs["speaker"] = self._speaker
                    mode = f"builtin:{self._speaker}"

                self._tts.tts_to_file(**kwargs)  # type: ignore[union-attr]
                data = out_path.read_bytes()
                log.info(
                    "TTS[%s]: %d chars → %d KB em %.2fs",
                    mode, len(text), len(data) // 1024, time.time() - t0,
                )
                return data
            finally:
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass


# Instância única do processo
ENGINE = TTSEngine()


def synthesize(text: str, language: str = "pt") -> bytes:
    """Atalho para ENGINE.synthesize."""
    return ENGINE.synthesize(text, language=language)
