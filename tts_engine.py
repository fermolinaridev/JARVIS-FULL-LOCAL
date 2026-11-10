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

import io
import logging
import os
import re
import tempfile
import threading
import time
import wave
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
    # Markdown e emojis que o LLM solta às vezes ("*CPU*", "😊") viram ruído falado.
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"(?m)^\s*[-•]\s+", "", text)   # marcador de lista
    text = re.sub(r"[🀀-🫿☀-➿️]", "", text)
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    return re.sub(r"\.+(\s+|$)", lambda m: "," + m.group(1) if m.group(1) else "", text).strip(" ,")


def _silence_wav(seconds: float = 0.05, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


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
        # Cache do timbre clonado: (caminho, mtime) -> latentes do XTTS.
        self._latents_key: Optional[tuple] = None
        self._latents: Optional[tuple] = None

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

    def _clone_latents(self, sample: Path) -> tuple:
        """Latentes da voz de referência — calculados 1x e reusados (antes eram
        recalculados a cada fala, ~0,5 s jogados fora por frase)."""
        key = (str(sample), sample.stat().st_mtime_ns)
        if self._latents_key != key:
            model = self._tts.synthesizer.tts_model  # type: ignore[union-attr]
            cfg = model.config
            self._latents = model.get_conditioning_latents(
                audio_path=[str(sample)],
                gpt_cond_len=cfg.gpt_cond_len,
                gpt_cond_chunk_len=cfg.gpt_cond_chunk_len,
                max_ref_length=cfg.max_ref_len,
                sound_norm_refs=cfg.sound_norm_refs,
            )
            self._latents_key = key
        return self._latents  # type: ignore[return-value]

    def _synth_clone(self, text: str, language: str, sample: Path) -> bytes:
        """Clone direto pelo modelo XTTS (latentes em cache, WAV em memória)."""
        import numpy as np

        model = self._tts.synthesizer.tts_model  # type: ignore[union-attr]
        cfg = model.config
        gpt_cond, speaker_emb = self._clone_latents(sample)
        out = model.inference(
            text, language, gpt_cond, speaker_emb,
            temperature=cfg.temperature,
            length_penalty=cfg.length_penalty,
            repetition_penalty=cfg.repetition_penalty,
            top_k=cfg.top_k,
            top_p=cfg.top_p,
            enable_text_splitting=True,
        )
        wav = np.clip(np.asarray(out["wav"], dtype=np.float32), -1.0, 1.0)
        # Devolve o cache do PyTorch pra GPU: sem isso o processo cresce de
        # ~2,4 pra ~2,9 GB e empurra o LLM pra memória compartilhada (lento).
        if self._device == "cuda":
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(cfg.audio.output_sample_rate))
            w.writeframes((wav * 32767.0).astype("<i2").tobytes())
        return buf.getvalue()

    def warmup(self) -> None:
        """Carrega o modelo, calcula o timbre e roda uma fala curta descartável,
        pra primeira fala de verdade não pagar a inicialização da GPU (~3 s)."""
        sample = _resolve_sample()
        with self._lock:
            self._ensure_loaded()
            if sample:
                try:
                    self._synth_clone("Pronto, Senhor", "pt", sample)
                except Exception:  # noqa: BLE001
                    log.exception("TTS: warmup falhou (segue sem)")

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
        if not re.search(r"\w", text):
            # Só emoji/pontuação (ex.: "😊" no fim da resposta): nada a falar.
            return _silence_wav()

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
            if sample:
                try:
                    data = self._synth_clone(text, language, sample)
                    log.info(
                        "TTS[clone]: %d chars → %d KB em %.2fs",
                        len(text), len(data) // 1024, time.time() - t0,
                    )
                    return data
                except Exception:  # noqa: BLE001
                    # API interna mudou? Cai pro caminho público (mais lento).
                    log.exception("TTS: clone direto falhou — usando tts_to_file")

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
