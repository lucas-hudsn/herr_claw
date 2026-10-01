"""STT per the P0 verdict (SPEC §3): mic → mlx-whisper `base`, language
locked to `de`, 16 kHz mono via sounddevice → text.

P0 findings baked in here:
- Model `base`, never `tiny` — tiny's German accuracy is too poor
  (AGENTS.md); base stays real-time on Apple Silicon. mlx_whisper's
  ModelHolder caches the loaded model; warm() must run in the SAME thread
  that later transcribes — MLX streams are thread-local, and a background-
  thread warm poisons inference in the main thread (reproduced: "There is
  no Stream(cpu, 1) in current thread", then a segfault). The sprechen loop
  therefore warms synchronously at startup (~1 s, before the first press).
- The iPhone Continuity mic shows up as a regular input but returns pure
  digital silence — pick_input_device skips iPhone/iPad/Continuity devices
  (HERR_MIC_DEVICE wins over everything, by name or index).
- Recording stops ~1.2 s after speech ends (5 s cap), so a 2–3 s utterance
  round-trips well under the 5 s target (SPEC §4.2).
"""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16_000
DEFAULT_MODEL = "base"
DEFAULT_MAX_SECONDS = 5.0

# Whisper shortcut names are not valid HF repo ids — mlx_whisper needs the
# full conversion repo (its own CLI hardcodes the same mapping).
_MLX_REPO_FMT = "mlx-community/whisper-{name}-mlx"

# P0: these deliver pure digital silence on this Mac — never pick them
# unless explicitly asked via HERR_MIC_DEVICE.
_GHOST_MIC_SUBSTRINGS = ("iphone", "ipad", "continuity")

_MIC_HINT = (
    "Mikrofon nicht verfügbar — Terminal-Mikrofonfreigabe prüfen (TCC; "
    "Ghostty ist erlaubt) und ggf. HERR_MIC_DEVICE setzen."
)


class AudioError(RuntimeError):
    """Audio hardware/config problem, message is user-facing (German)."""


def resolve_model(name: str = DEFAULT_MODEL) -> str:
    """`base` → mlx-community/whisper-base-mlx; repo ids/paths pass through."""
    name = (name or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if "/" in name or name.startswith(".") or os.path.exists(name):
        return name
    return _MLX_REPO_FMT.format(name=name)


def pick_input_device(
    devices,
    default_input: int | None = None,
    override: str = "",
) -> int | None:
    """Choose the PortAudio input device (P0: never a Continuity ghost mic).

    Order: HERR_MIC_DEVICE (name substring or index, must exist) → system
    default input when it is a real mic → first real mic → the default even
    if it looks like a ghost (last resort). None when no input exists."""
    inputs = [
        (i, d)
        for i, d in enumerate(devices)
        if int(d.get("max_input_channels", 0)) > 0
    ]
    if not inputs:
        return None
    by_index = dict(inputs)

    def _ghost(d) -> bool:
        name = str(d.get("name", "")).lower()
        return any(s in name for s in _GHOST_MIC_SUBSTRINGS)

    if override:
        ov = override.strip()
        if ov.isdigit() and int(ov) in by_index:
            return int(ov)
        lowered = ov.lower()
        for i, d in inputs:
            if lowered in str(d.get("name", "")).lower():
                return i
        raise AudioError(
            f"Mikrofon „{override}“ (HERR_MIC_DEVICE) nicht gefunden — "
            "verfügbare Eingaben: "
            + ", ".join(str(d.get("name", "?")) for _, d in inputs)
        )
    if default_input in by_index and not _ghost(by_index[default_input]):
        return default_input
    for i, d in inputs:
        if not _ghost(d):
            return i
    return default_input if default_input in by_index else inputs[0][0]


@dataclass
class Recording:
    audio: np.ndarray  # float32 mono @ SAMPLE_RATE, speech trimmed in
    speech_detected: bool
    seconds: float  # wall time captured (before trimming)


def record(
    *,
    device: int | None = None,
    samplerate: int = SAMPLE_RATE,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    chunk_seconds: float = 0.1,
    silence_tail: float = 1.2,
    preroll_seconds: float = 0.25,
    min_speech_rms: float = 0.004,
    speech_rms_factor: float = 4.0,
    absolute_speech_rms: float = 0.02,
    stream_factory=None,
) -> Recording:
    """Record until speech ends (silence for `silence_tail`) or `max_seconds`.

    Speech gate: RMS above max(min_speech_rms, speech_rms_factor × noise
    floor); the floor only learns from chunks below `absolute_speech_rms`, so
    speaking the instant the key is pressed cannot poison the threshold.
    Leading silence is trimmed except `preroll_seconds`. In a loud room,
    speak from within arm's reach — the gate needs the level edge.
    """
    if stream_factory is None:
        import sounddevice as sd

        stream_factory = sd.InputStream
    blocksize = max(1, int(samplerate * chunk_seconds))
    stream = None
    chunks: list[np.ndarray] = []
    floor: float | None = None
    speech_at: int | None = None
    silence = 0.0
    try:
        stream = stream_factory(
            samplerate=samplerate,
            blocksize=blocksize,
            channels=1,
            dtype="float32",
            device=device,
        )
        stream.start()
        while len(chunks) * chunk_seconds < max_seconds:
            data = stream.read(blocksize)
            if isinstance(data, tuple):
                data = data[0]
            if data is None or len(data) == 0:
                break
            chunk = np.asarray(data, dtype=np.float32).reshape(-1)
            chunks.append(chunk)
            rms = float(np.sqrt(np.mean(np.square(chunk))))
            # the floor only learns from plausibly-silent chunks — speech must
            # never raise it (or its own threshold)
            if rms < absolute_speech_rms and (floor is None or rms < floor):
                floor = rms
            floor_value = floor if floor is not None else 0.0
            threshold = max(min_speech_rms, speech_rms_factor * floor_value)
            if speech_at is None:
                if rms > threshold:
                    speech_at = len(chunks) - 1
            elif rms <= threshold:
                silence += chunk_seconds
                if silence >= silence_tail:
                    break
            else:
                silence = 0.0
    except OSError as exc:
        raise AudioError(_MIC_HINT) from exc
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # already-failed stream — nothing to save
                pass

    seconds = len(chunks) * chunk_seconds
    if speech_at is None:
        return Recording(
            audio=np.zeros(0, dtype=np.float32), speech_detected=False, seconds=seconds
        )
    start = max(0, speech_at - int(round(preroll_seconds / chunk_seconds)))
    audio = np.concatenate(chunks[start:]).astype(np.float32)
    return Recording(audio=audio, speech_detected=True, seconds=seconds)


class Mic:
    """Input-device selection + capture, set up once per sprechen run."""

    def __init__(self, override: str = "", **record_kwargs) -> None:
        import sounddevice as sd

        devices = sd.query_devices()
        try:
            default_input = int(sd.default.device[0])
        except (TypeError, ValueError, IndexError):
            default_input = None
        if default_input is not None and default_input < 0:
            default_input = None
        self.device = pick_input_device(
            devices, default_input=default_input, override=override
        )
        self._record_kwargs = record_kwargs

    def record(self) -> Recording:
        return record(device=self.device, **self._record_kwargs)


@contextlib.contextmanager
def _no_progress_bars():
    """mlx_whisper draws tqdm mel/decode bars on stderr every call — hide
    them so they don't clobber the TUI (errors still arrive as exceptions)."""
    with contextlib.redirect_stderr(io.StringIO()):
        yield


class Transcriber:
    """mlx-whisper wrapper: float32 mono in, German text out.

    The mlx_whisper import is deferred — only voice mode pays for it. The
    model itself is cached inside mlx_whisper (ModelHolder), so repeated
    calls after warm() are inference-only.
    """

    def __init__(self, model: str = DEFAULT_MODEL, language: str = "de") -> None:
        self.repo = resolve_model(model)
        self.language = language  # locked to de (AGENTS.md)

    def __call__(self, audio: np.ndarray) -> str:
        if audio is None or len(audio) == 0:
            return ""
        import mlx_whisper

        with _no_progress_bars():
            result = mlx_whisper.transcribe(
                np.asarray(audio, dtype=np.float32),
                path_or_hf_repo=self.repo,
                language=self.language,
                temperature=0.0,  # deterministic — a tutor must not guess
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                verbose=False,
            )
        return " ".join(
            str(seg.get("text", "")).strip() for seg in result.get("segments", [])
        ).strip()

    def warm(self) -> None:
        """Load the model now. Call this from the thread that will transcribe
        (MLX streams are thread-local) — the sprechen loop does it once at
        startup so the first turn pays only inference, not the load. Raises
        on failure (download errors etc.) so the caller can warn."""
        self(np.zeros(SAMPLE_RATE // 10, dtype=np.float32))


def transcribe_file(path, model: str = DEFAULT_MODEL) -> str:
    """Smoke-test helper: transcribe a 16 kHz mono wav (e.g. the `say`-made
    audio/test_de.wav) without touching the mic."""
    import soundfile as sf

    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if sr != SAMPLE_RATE:
        raise AudioError(
            f"{path}: {sr} Hz — bitte 16 kHz liefern (say --data-format=LEI16@{SAMPLE_RATE})."
        )
    return Transcriber(model)(audio.mean(axis=1))
