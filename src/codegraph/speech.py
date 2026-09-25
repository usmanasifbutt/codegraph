"""Local speech-to-text with faster-whisper (design D7; same setup as ai-lab/voice-notes).

Audio stays in memory and never leaves the machine. The model is expensive to build, so callers
cache `build_model()` themselves (the UI uses `st.cache_resource`).
"""

from __future__ import annotations

import io
import wave
from typing import Any

from codegraph.config import SpeechSettings

MAX_SECONDS = 60
MAX_BYTES = 10 * 1024 * 1024


class AudioError(Exception):
    """The recording is unusable (too long, too big, undecodable)."""


class NoSpeechError(AudioError):
    def __init__(self) -> None:
        super().__init__("No speech detected")


def build_model(settings: SpeechSettings) -> Any:
    """Load (downloading on first use) the Whisper model. Raises on failure."""
    from faster_whisper import WhisperModel

    return WhisperModel(
        settings.whisper_model, device=settings.device, compute_type=settings.compute_type
    )


def wav_duration(data: bytes) -> float | None:
    """Duration in seconds for WAV data; None when it is not a readable WAV file."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, EOFError):
        return None


def check_audio(data: bytes) -> None:
    if not data:
        raise NoSpeechError()
    if len(data) > MAX_BYTES:
        raise AudioError(f"Recordings are limited to {MAX_BYTES // (1024 * 1024)} MB.")
    duration = wav_duration(data)
    if duration is not None and duration > MAX_SECONDS:
        raise AudioError(f"Recordings are limited to {MAX_SECONDS} seconds.")


def transcribe(model: Any, data: bytes) -> str:
    """Transcribe in-memory audio; raises NoSpeechError for silence, AudioError on failure."""
    check_audio(data)
    try:
        segments, _info = model.transcribe(io.BytesIO(data))
        text = " ".join(s.text.strip() for s in segments).strip()
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError(f"Transcription failed: {type(exc).__name__}: {exc}") from exc
    if not text:
        raise NoSpeechError()
    return text


class ModelLoader:
    """Loads the model at most once per process and remembers a load failure (design D7)."""

    def __init__(self, settings: SpeechSettings, build=build_model) -> None:
        import threading

        self.settings, self._build = settings, build
        self._lock = threading.Lock()
        self._model: Any = None
        self.error: str | None = None
        self.loads = 0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def get(self) -> Any:
        """Return the model, loading it on first use; raises AudioError if it can't load."""
        with self._lock:
            if self._model is None and self.error is None:
                self.loads += 1
                try:
                    self._model = self._build(self.settings)
                except Exception as exc:
                    self.error = (
                        f"Voice input is unavailable: could not load Whisper model "
                        f"'{self.settings.whisper_model}' ({type(exc).__name__}: {exc})"
                    )
            if self.error:
                raise AudioError(self.error)
            return self._model
