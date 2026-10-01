"""voice.stt — device selection (P0 ghost-mic rule), energy-gated recording,
Transcriber glue, and the opt-in live transcription smoke test."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from voice.stt import (
    AudioError,
    Transcriber,
    pick_input_device,
    record,
    resolve_model,
    transcribe_file,
)

DEVICES = [
    {"name": "MacBook Pro Microphone", "max_input_channels": 1},
    {"name": "MacBook Pro Speakers", "max_input_channels": 0},
    {"name": "Lucas’s iPhone Microphone", "max_input_channels": 1},
]


# -- resolve_model -----------------------------------------------------------

def test_short_names_map_to_mlx_community_repos():
    assert resolve_model("base") == "mlx-community/whisper-base-mlx"
    assert resolve_model("tiny") == "mlx-community/whisper-tiny-mlx"


def test_repo_ids_and_paths_pass_through_and_default_is_base():
    assert (
        resolve_model("mlx-community/whisper-large-v3-turbo-mlx")
        == "mlx-community/whisper-large-v3-turbo-mlx"
    )
    assert resolve_model("some/dir") == "some/dir"
    assert resolve_model("") == "mlx-community/whisper-base-mlx"


# -- pick_input_device -------------------------------------------------------

def test_default_input_used_when_real():
    assert pick_input_device(DEVICES, default_input=0) == 0


def test_iphone_continuity_mic_is_skipped_when_default():
    # P0: the iPhone Continuity mic returns pure digital silence.
    assert pick_input_device(DEVICES, default_input=2) == 0


def test_explicit_override_wins_even_if_ghost():
    assert pick_input_device(DEVICES, default_input=0, override="iPhone") == 2
    assert pick_input_device(DEVICES, default_input=2, override="macbook") == 0
    assert pick_input_device(DEVICES, default_input=0, override="2") == 2


def test_unknown_override_fails_loudly():
    with pytest.raises(AudioError):
        pick_input_device(DEVICES, default_input=0, override="Studio Display")


def test_no_input_devices_returns_none():
    outputs = [{"name": "Speakers", "max_input_channels": 0}]
    assert pick_input_device(outputs, default_input=None) is None


def test_ghost_only_setup_falls_back_to_default():
    ghosts = [{"name": "iPhone Microphone", "max_input_channels": 1}]
    assert pick_input_device(ghosts, default_input=0) == 0


# -- record (energy gate via a fake stream) ----------------------------------

class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def start(self):
        pass

    def read(self, n):
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        return self._chunks.pop(0)

    def stop(self):
        pass

    def close(self):
        pass


def stream_factory_for(chunks):
    return lambda **kwargs: FakeStream(chunks)


def chunk(level: float, seconds: float = 0.1, samplerate: int = 16000) -> np.ndarray:
    """0.1 s chunk with (approximately) the requested RMS — matches the
    recorder's blocksize at the default 16 kHz."""
    n = int(samplerate * seconds)
    wave = np.full(n, level, dtype=np.float32)
    return wave


QUIET, LOUD = 0.0005, 0.05


def test_record_stops_after_silence_tail_and_trims_leading_silence():
    script = [chunk(QUIET) for _ in range(5)]  # 0.5 s before speaking
    script += [chunk(LOUD) for _ in range(3)]  # ~0.3 s speech
    script += [chunk(QUIET) for _ in range(12)]  # 1.2 s tail → stop

    rec = record(stream_factory=stream_factory_for(script))

    assert rec.speech_detected is True
    assert rec.seconds == pytest.approx(2.0, abs=0.05)  # stopped early, not 5 s
    assert len(rec.audio) == pytest.approx(1.7 * 16000, abs=0.1 * 16000)


def test_record_without_speech_returns_empty_after_cap():
    script = [chunk(QUIET) for _ in range(60)]  # longer than the 5 s cap
    rec = record(stream_factory=stream_factory_for(script))
    assert rec.speech_detected is False
    assert len(rec.audio) == 0
    assert rec.seconds == pytest.approx(5.0, abs=0.2)


def test_record_caps_continuous_speech_at_max_seconds():
    script = [chunk(LOUD) for _ in range(80)]
    rec = record(stream_factory=stream_factory_for(script))
    assert rec.speech_detected is True
    assert rec.seconds == pytest.approx(5.0, abs=0.2)


def test_record_translates_stream_errors():
    def broken(**kwargs):
        raise OSError("no device")

    with pytest.raises(AudioError):
        record(stream_factory=broken)


# -- Transcriber (mlx_whisper faked) -----------------------------------------

class FakeMLX(SimpleNamespace):
    calls: list = []

    @staticmethod
    def transcribe(audio, **kwargs):
        FakeMLX.calls.append(kwargs)
        return {"segments": [{"text": "Hallo "}, {"text": "Welt!"}]}


@pytest.fixture
def fake_mlx(monkeypatch):
    FakeMLX.calls = []
    monkeypatch.setitem(sys.modules, "mlx_whisper", FakeMLX)
    return FakeMLX


def test_transcriber_joins_segments_and_passes_p0_settings(fake_mlx):
    text = Transcriber()(np.zeros(1600, dtype=np.float32))
    assert text == "Hallo Welt!"
    assert len(fake_mlx.calls) == 1
    call = fake_mlx.calls[0]
    assert call["language"] == "de"
    assert call["path_or_hf_repo"] == "mlx-community/whisper-base-mlx"
    assert call["temperature"] == 0.0


def test_transcriber_empty_audio_short_circuits(fake_mlx):
    assert Transcriber()(np.zeros(0, dtype=np.float32)) == ""
    assert fake_mlx.calls == []


def test_transcriber_model_override(fake_mlx):
    Transcriber(model="tiny")(np.zeros(16, dtype=np.float32))
    assert fake_mlx.calls[0]["path_or_hf_repo"] == "mlx-community/whisper-tiny-mlx"


# -- live smoke (opt-in: downloads/loads whisper-base-mlx) --------------------

@pytest.mark.skipif(
    os.environ.get("HERR_LIVE_STT") != "1",
    reason="live STT needs the whisper-base-mlx model — run with HERR_LIVE_STT=1",
)
@pytest.mark.skipif(not Path("audio/test_de.wav").exists(), reason="audio/test_de.wav fehlt")
def test_live_stt_transcribes_the_german_sample():
    text = transcribe_file("audio/test_de.wav")
    lowered = text.lower()
    assert "berlin" in lowered or "deutsch" in lowered, text
