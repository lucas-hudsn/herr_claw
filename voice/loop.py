"""`herr-claw sprechen` — the voice loop (P3, SPEC §4.2).

push-to-talk (Leertaste/⏎) → record (silence-stopped, ≤5 s) → STT
(mlx-whisper base, de) → Nemotron reply (thinking off, sanitized) → print
+ `say -v Anna`. Same ONE state path and tracking as `chat`: SrsState,
SessionState, Tracker, and the stable slash commands via
agent.commands.dispatch (command output is printed, never spoken). The
round-trip latency is printed per turn so the <5 s budget is visible live.
"""

from __future__ import annotations

import os
import sys
import termios
import time
import tty
from datetime import datetime

from agent.bridge import BridgeClient
from agent.chat import _topic_from_calendar, load_soul
from agent.commands import ChatContext, Direct, ToLLM, dispatch
from agent.config import Config, load_config
from agent.llm import LLMError, Tutor, make_client, parse_correction
from agent.state import SessionState, SrsState
from agent.tracker import Tracker

from .stt import AudioError, Mic, Transcriber
from .tts import Speaker

MAX_HISTORY_MESSAGES = 12  # same window as chat
VOICE_MAX_TOKENS = 300  # 3–4 short German sentences; caps runaway replies
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
    cfg: Config | None = None,
    *,
    tutor: Tutor | None = None,
    transcribe=None,  # ndarray → str
    speaker: Speaker | None = None,
    record_audio=None,  # → ndarray (default: Mic)
    key_prompt: KeyPrompt | None = None,
    output_fn=print,
) -> int:
    cfg = cfg or load_config()
    srs = SrsState(cfg.srs_path)
    session = SessionState.load(cfg.session_path)
    vault = None
    if cfg.vault_root:
        from herrclaw_bridge.vault import Vault

        vault = Vault(cfg.vault_root)
    else:
        output_fn("⚠︎ HERR_VAULT ist nicht gesetzt — Obsidian-Notizen sind deaktiviert.")
    if tutor is None:
        try:
            tutor = Tutor(
                make_client(cfg.base_url),
                model=cfg.model,
                system_prompt=load_soul(),
                max_tokens=VOICE_MAX_TOKENS,
            )
        except LLMError as exc:
            output_fn(f"⚠︎ {exc}")
            return 1
    bridge = BridgeClient(cfg.bridge_url) if cfg.bridge_url else None
    tracker = Tracker(vault=vault, srs=srs)
    ctx = ChatContext(srs=srs, session=session, tracker=tracker, bridge=bridge)

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
    startup_note = _topic_from_calendar(bridge, session, output_fn)
    history: list[dict[str, str]] = []

    def tutor_exchange(user_message: str, system_note: str | None = None, speak: bool = True) -> str | None:
        nonlocal startup_note
        if system_note is None and startup_note:
            system_note, startup_note = startup_note, None
        history.append({"role": "user", "content": user_message})
        try:
            reply = tutor.reply(history[-MAX_HISTORY_MESSAGES:], system_note=system_note)
        except LLMError as exc:
            output_fn(f"⚠︎ {exc}")
            history.pop()  # keep the window clean; the user simply retries
            return None
        history.append({"role": "assistant", "content": reply})
        output_fn(f"Herr Claw: {reply}")
        tracker.note_exchange()
        srs.add_message()
        srs.touch_day()
        correction = parse_correction(reply)
        if correction:
            fix, example = correction
            tracker.log_correction(example=example, fix=fix)
        if speak:
            speaker.say(reply)
        return reply

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
        reply = tutor_exchange(transcript, speak=False)
        if reply is not None:
            # Anna's playback time is intentionally not counted — this is the
            # response latency the <5s budget (SPEC §4.2) is about.
            output_fn(f"⏱ {time.monotonic() - captured:.1f} s bis zur Antwort")
            speaker.say(reply)

    prompt = key_prompt or KeyPrompt()
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
        outcome = dispatch(line, ctx)
        if isinstance(outcome, Direct):
            output_fn(f"Herr Claw: {outcome.text}")  # command output stays on screen
            continue
        if isinstance(outcome, ToLLM):
            tutor_exchange(outcome.user_message, system_note=outcome.system_note)
        else:
            tutor_exchange(line)

    tracker.end_session(topic=session.current_topic)
    session.last_session_turns = tracker.session_turns
    session.last_session_end = datetime.now().isoformat(timespec="seconds")
    srs.save()
    session.save()
    if tracker.session_turns:
        output_fn("Tschüss! Bis zum nächsten Mal. 👋")
    return 0
