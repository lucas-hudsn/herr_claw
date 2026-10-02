"""`herr-claw sprechen` — the voice loop (P3, SPEC §4.2; P6 thin client).

push-to-talk (Leertaste/⏎) → record (silence-stopped, ≤5 s) → STT
(mlx-whisper base, de) → the brain's reply → print + `say -v Anna`.
STT and TTS stay host-side (macOS audio); the BRAIN is the same seam as
the TUI's: sandbox brain by default, local break-glass fallback
(agent.turns.AutoBrain). Command output arrives marked `speakable=False`
and is printed, never spoken. The round-trip latency is printed per turn
so the <5 s budget is visible live.
"""

from __future__ import annotations

import os
import sys
import termios
import time
import tty

from .stt import AudioError, Mic, Transcriber
from .tts import Speaker

QUIT_WORDS = {"q", "quit", "exit", "beenden", "tschüss", "tschuess"}

BANNER = (
    "Herr Claw 🎙️ — Sprachmodus (A1–A2)\n"
    "Leertaste/⏎ = sprechen (max 5 s) · Befehl + ⏎ (/fehler, /quiz, …) · q + ⏎ = beenden"
)

PROMPT = "🎙️  Leertaste/⏎ = sprechen · Befehl+⏎ · q+⏎ = Ende › "


def _termios_key() -> str:
    """One keypress in cbreak mode (Enter arrives as '\\r'); termios is
    restored after every read so Ctrl-C works normally during LLM/TTS."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        data = os.read(fd, 256)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if not data:
        return "\x04"  # EOF (Ctrl-D)
    return data.decode("utf-8", errors="replace")


def _piped_line() -> str:
    line = sys.stdin.readline()
    return line if line.endswith("\n") else line + "\n"


def _default_reader() -> str:
    if sys.stdin.isatty():
        return _termios_key()
    return _piped_line()


class KeyPrompt:
    """Single-key terminal prompt: SPACE on an empty line (or an empty ⏎)
    fires on_space() and keeps prompting; printable keys build a line,
    ⏎ submits it, ⌫ edits, Ctrl-C/D quit (→ None). Non-TTY stdin falls
    back to input() lines with the same semantics."""

    def __init__(self, writer=None, reader=None) -> None:
        self._write = writer or (lambda s: print(s, end="", flush=True))
        self._reader = reader or _default_reader

    def read(self, on_space) -> str | None:
        buffer = ""
        self._write(PROMPT)
        while True:
            try:
                ch = self._reader()
            except KeyboardInterrupt:  # SIGINT between keys
                self._write("\n")
                return None
            if ch in ("\x03", "\x04", ""):
                self._write("\n")
                return None
            if ch in ("\r", "\n"):
                self._write("\n")
                if buffer:
                    return buffer
                on_space()  # empty ⏎ = sprechen (same as space)
                buffer = ""
                self._write(PROMPT)
                continue
            if ch in ("\b", "\x7f"):
                if buffer:
                    buffer = buffer[:-1]
                    self._write("\b \b")
                continue
            if ch == "\x1b":  # arrow/function key — swallow its 2 param bytes
                self._reader()
                self._reader()
                continue
            if ch == " " and not buffer:
                self._write("\n")
                on_space()
                self._write(PROMPT)
                continue
            if ch.isprintable():
                buffer += ch
                self._write(ch)


def run_sprechen(
    cfg=None,
    *,
    brain=None,
    transcribe=None,  # ndarray → str
    speaker: Speaker | None = None,
    record_audio=None,  # → ndarray (default: Mic)
    key_prompt: KeyPrompt | None = None,
    output_fn=print,
) -> int:
    from agent.llm import LLMError
    from agent.turns import TurnError, make_brain

    cfg = cfg or load_config()
    if brain is None:
        try:
            brain = make_brain(cfg, output_fn)
        except LLMError as exc:
            output_fn(f"⚠︎ {exc}")
            return 1

    if transcribe is None:
        stt = Transcriber(cfg.whisper_model)
        # Synchronous warm — MLX streams are thread-local, so a background
        # warm would poison inference in this thread (see voice/stt.py).
        output_fn("⏳ Lade Whisper (einmalig, ~1 s) …")
        try:
            stt.warm()
        except Exception as exc:
            output_fn(f"⚠︎ Whisper-Modell nicht geladen ({type(exc).__name__}: {exc}).")
        transcribe = stt  # wire the default into the call site
    if speaker is None:
        speaker = Speaker()
        if not speaker.available():
            output_fn("⚠︎ `say` nicht gefunden — Antworten werden nur angezeigt.")

    if record_audio is None:
        try:
            mic = Mic(override=cfg.mic_device)
            if mic.device is not None:
                record_audio = lambda: mic.record().audio  # noqa: E731 — local closure
            else:
                output_fn("⚠︎ Kein Eingabemikrofon gefunden — tippen funktioniert weiterhin.")
        except AudioError as exc:
            output_fn(f"⚠︎ {exc}")

    output_fn(BANNER)

    def speak_line(text: str) -> None:
        output_fn(f"Herr Claw: {text}")
        speaker.say(text)

    def brain_exchange(text: str, speak: bool = True) -> str | None:
        try:
            result = brain.turn(text)
        except (TurnError, LLMError) as exc:
            output_fn(f"⚠︎ {exc}")
            return None
        if result.speakable and speak:
            speak_line(result.reply)
        else:
            output_fn(f"Herr Claw: {result.reply}")  # command output stays on screen
        return result.reply

    def voice_turn() -> None:
        if record_audio is None:
            output_fn("⚠︎ Kein Mikrofon — tippe einfach los.")
            return
        output_fn("🔴 Ich höre zu … (Sprachende wird erkannt, max 5 s)")
        try:
            audio = record_audio()
        except AudioError as exc:
            output_fn(f"⚠︎ {exc}")
            return
        captured = time.monotonic()  # reply latency counts from end of speech
        if audio is None or len(audio) == 0:
            output_fn("🤫 Ich habe nichts gehört — nochmal, bitte!")
            return
        try:
            transcript = transcribe(audio)
        except AudioError as exc:
            output_fn(f"⚠︎ {exc}")
            return
        except Exception as exc:  # whisper/model hiccup — stay in the loop
            output_fn(f"⚠︎ Spracherkennung fehlgeschlagen ({type(exc).__name__}: {exc}).")
            return
        if not transcript:
            output_fn("🤔 Das habe ich nicht verstanden — nochmal, bitte!")
            speaker.say("Das habe ich nicht verstanden. Nochmal, bitte!")
            return
        output_fn(f"Du 🗣️: {transcript}")
        reply = brain_exchange(transcript, speak=False)
        if reply is not None:
            # Anna's playback time is intentionally not counted — this is the
            # response latency the <5s budget (SPEC §4.2) is about.
            output_fn(f"⏱ {time.monotonic() - captured:.1f} s bis zur Antwort")
            speaker.say(reply)

    prompt = key_prompt or KeyPrompt()
    turns = 0
    try:
        while True:
            try:
                line = prompt.read(on_space=voice_turn)
            except KeyboardInterrupt:  # Ctrl-C while `say`/LLM runs
                output_fn("")
                break
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            if line.lower() in QUIT_WORDS:
                break
            if brain_exchange(line) is not None:
                turns += 1
    finally:
        brain.close()
    if turns:
        output_fn("Tschüss! Bis zum nächsten Mal. 👋")
    return 0
