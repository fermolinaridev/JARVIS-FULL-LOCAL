"""
JARVIS — engine de TTS local com clonagem de voz via Coqui XTTS-v2.

Estratégia:
  - Carrega o modelo XTTS-v2 sob demanda (lazy) — ~1.8 GB de download na 1ª vez.
  - Usa uma amostra .wav em samples/ como referência de timbre (clonagem).
  - Sintetiza pra um arquivo WAV temporário e devolve os bytes.
  - Lock global porque o XTTS não é seguro pra chamadas paralelas.
  - Se torch/TTS não estiver disponível, a engine fica em modo "indisponível"
    e o /api/tts devolve 503 — o frontend cai pra Web Speech API.

Modelo:
  tts_models/multilingual/multi-dataset/xtts_v2
  - Multilíngue (inclui pt)
  - Faz clonagem zero-shot a partir de ~6s de áudio de referência
  - Inferência ~1x realtime em GPU mid-range (RTX 5060), ~5-8x em CPU
"""
from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.tts")

# Aceita o ToS do Coqui automaticamente (evita prompt interativo)
os.environ.setdefault("COQUI_TOS_AGREED", "1")


# Caminhos
ROOT = Path(__file__).resolve().parent
SAMPLES_DIR = ROOT / "samples"
DEFAULT_SAMPLE_NAME = "jarvis_voice.wav"
MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"


def _resolve_sample() -> Optional[Path]:
    """
    Resolve a amostra de voz pra clonagem.
    Procura por:
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

    @property
    def available(self) -> bool:
        """True se já carregou OU se ainda dá pra tentar carregar."""
        return self._load_error is None

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def status(self) -> dict:
        return {
            "loaded": self._loaded,
            "device": self._device,
            "error": self._load_error,
            "sample": str(self._sample_path) if self._sample_path else None,
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
                gpu_name = torch.cuda.get_device_name(0)
                log.info("TTS: GPU detectada (%s)", gpu_name)
            else:
                self._device = "cpu"
                log.info("TTS: rodando em CPU (mais lento)")
        except Exception:
            self._device = "cpu"

        # Carrega o modelo (pode demorar 30-60s na primeira vez por causa do download)
        t0 = time.time()
        log.info("TTS: carregando %s em %s ...", MODEL_NAME, self._device)
        try:
            self._tts = TTS(model_name=MODEL_NAME, progress_bar=False).to(self._device)
        except Exception as e:
            self._load_error = f"falha ao carregar XTTS-v2: {e}"
            log.exception("TTS: falha ao carregar modelo")
            raise RuntimeError(self._load_error) from e

        self._sample_path = _resolve_sample()
        if not self._sample_path:
            log.warning(
                "TTS: nenhuma amostra de voz em %s — coloque um .wav lá pra clonar timbre",
                SAMPLES_DIR,
            )

        self._loaded = True
        log.info("TTS: pronto em %.1fs (sample=%s)", time.time() - t0, self._sample_path)

    def synthesize(self, text: str, language: str = "pt") -> bytes:
        """
        Sintetiza `text` no idioma `language` (default pt) e devolve WAV em bytes.
        Levanta RuntimeError se a engine não pôde ser carregada.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("texto vazio")

        # XTTS limita a 250 chars/chunk; o wrapper divide automaticamente,
        # mas pra latência razoável a gente trunca em 800 chars (~30s de fala).
        if len(text) > 800:
            text = text[:797] + "..."

        # Resolve a amostra ANTES de baixar 1.8 GB de modelo — assim, sem
        # sample, a gente devolve 503 imediato e o front cai pra Web Speech.
        sample = _resolve_sample()
        if not sample:
            raise RuntimeError(
                "amostra de voz ausente: coloque um .wav em samples/jarvis_voice.wav"
            )

        with self._lock:
            self._sample_path = sample
            self._ensure_loaded()

            t0 = time.time()
            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, dir=str(ROOT / ".cache" if (ROOT / ".cache").exists() else None),
            ) as tmp:
                out_path = Path(tmp.name)
            try:
                self._tts.tts_to_file(  # type: ignore[union-attr]
                    text=text,
                    speaker_wav=str(self._sample_path),
                    language=language,
                    file_path=str(out_path),
                )
                data = out_path.read_bytes()
                log.info("TTS: %d chars → %d KB em %.2fs", len(text), len(data) // 1024, time.time() - t0)
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
