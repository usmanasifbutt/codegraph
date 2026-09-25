import io
import os
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from codegraph.config import SpeechSettings
from codegraph.speech import (
    MAX_BYTES,
    AudioError,
    NoSpeechError,
    build_model,
    check_audio,
    transcribe,
)

AUDIO_DIR = Path(__file__).parent / "fixtures" / "audio"


def make_wav(seconds: float, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


class StubModel:
    def __init__(self, texts=None, exc=None):
        self.texts, self.exc, self.calls = texts or [], exc, 0

    def transcribe(self, fileobj):
        self.calls += 1
        assert isinstance(fileobj, io.BytesIO)  # in memory, never a path on disk
        if self.exc:
            raise self.exc
        return (SimpleNamespace(text=t) for t in self.texts), None


def test_short_clip_passes():
    check_audio(make_wav(2))
    assert transcribe(StubModel([" which modules ", " import requests "]), make_wav(2)) == (
        "which modules import requests"
    )


def test_too_long():
    with pytest.raises(AudioError, match="60 seconds"):
        check_audio(make_wav(61))


def test_too_big():
    with pytest.raises(AudioError, match="10 MB"):
        check_audio(b"\x00" * (MAX_BYTES + 1))


def test_silent_recording():
    with pytest.raises(NoSpeechError, match="No speech detected"):
        transcribe(StubModel(["  "]), make_wav(1))
    with pytest.raises(NoSpeechError):
        check_audio(b"")


def test_transcription_error():
    with pytest.raises(AudioError, match="Transcription failed: .*bad audio"):
        transcribe(StubModel(exc=ValueError("bad audio")), make_wav(1))


def test_no_files_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    before = set(os.listdir(tmp_path))
    transcribe(StubModel(["hello"]), make_wav(1))
    assert set(os.listdir(tmp_path)) == before


# -- 5.2 real model ---------------------------------------------------------------------
@pytest.mark.whisper
def test_real_tiny_model_transcribes_clip():
    clip = AUDIO_DIR / "which_modules.wav"
    model = build_model(SpeechSettings(whisper_model="tiny"))
    text = transcribe(model, clip.read_bytes()).lower()
    assert "module" in text and "import" in text, text


def test_model_loader_loads_once_and_remembers_failure():
    from codegraph.speech import ModelLoader

    built = []
    loader = ModelLoader(SpeechSettings(), build=lambda s: built.append(s) or object())
    first = loader.get()
    assert loader.get() is first and loader.loads == 1 and len(built) == 1

    def boom(_s):
        raise OSError("no network")

    failing = ModelLoader(SpeechSettings(whisper_model="small"), build=boom)
    for _ in range(2):
        with pytest.raises(AudioError, match="unavailable.*no network"):
            failing.get()
    assert failing.loads == 1 and failing.error


@pytest.mark.whisper
def test_real_model_second_use_does_not_reload():
    from codegraph.speech import ModelLoader

    loader = ModelLoader(SpeechSettings(whisper_model="tiny"))
    clip = (AUDIO_DIR / "which_modules.wav").read_bytes()
    assert transcribe(loader.get(), clip) and transcribe(loader.get(), clip)
    assert loader.loads == 1
