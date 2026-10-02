"""`herr-claw chat` — the text frontend (P1 loop, v0.5 real TUI).

On a TTY the loop runs as a small terminal UI: a fixed moustache-mascot
banner on top, the transcript scrolling between banner and a fixed status
line (terminal scroll region), agent turns marked with the moustache glyph
`:-{)` (SPEC §4.6 — the moustache is the product's face in every frontend).
Off-TTY (pipes, tests) it degrades to the plain read–reply loop.

Read–reply against Nemotron 3 with SOUL.md + the memory journal
(state/memory.md) as system prompt, rolling history, correction extraction,
and persistence: state/ always, Obsidian (via the bridge's scoped vault)
when HERR_VAULT is set.
"""

from __future__ import annotations

import sys
from datetime import datetime

from .bridge import BridgeClient, BridgeError
from .commands import ChatContext, Direct, ToLLM, dispatch, note_user_exchange
from .config import Config, SOUL_PATH, load_config
from .llm import LLMError, Tutor, make_client
from .memory import MemoryState
from .quiz import ensure_seeded
from .scheduling import fetch_today_events, suggest_topic
from .state import SessionState, SrsState
from .tracker import Tracker

MAX_HISTORY_MESSAGES = 12
MIN_TUI_ROWS = 14  # below this the moustache frame would not fit — plain loop

_FALLBACK_SOUL = (
    "Du bist Herr Claw, ein geduldiger Deutsch-Tutor für Lucas (A1-A2, Berlin). "
    "Antworte nur auf Deutsch in kurzen, einfachen Sätzen (max. 3-4), ohne Markdown, "
    "denn die Antworten werden laut vorgelesen. Nomen immer mit Artikel und Plural. "
    "Korrigiere Fehler sanft in einer Zeile: 'Richtig: … / Deine Version: …'. "
    "Erkläre auf Englisch nur, wenn Lucas darum bittet."
)

BANNER = """\
Herr Claw 🤖 — dein Deutsch-Tutor (A1–A2)
Befehle: /üben [Thema] · /quiz · /tag · /erfolge · /fehler · /fortschritt · /erkläre [Wort] · /pause [Tage] · /sprechen
Beenden: Strg-D
"""

# Canonical moustache mascot (SPEC §4.6) — this is the TUI's banner art.
MOUSTACHE = r'''
      .-"""-.
     /  o o  \
     |   ~   |
   .-' \___/ '-.
  (    )   (    )
   '--'|   |'--'
       '---'
'''

TUI_TITLE = "  Herr Claw — dein Deutsch-Tutor (A1–A2)"
TUI_COMMANDS = (
    "  /üben · /quiz · /tag · /erfolge · /fehler · /fortschritt · /erkläre · /pause · /sprechen"
)


def load_soul(path=SOUL_PATH) -> str:
    try:
        soul = path.read_text(encoding="utf-8").strip()
        if soul:
            return soul
    except OSError:
        pass
    return _FALLBACK_SOUL


def system_prompt_with_memory(memory: MemoryState | None) -> str:
    """SOUL + the memory journal — every content-generating prompt starts
    from this (SPEC §4.6); the memory tailors phrasing/topics only."""
    base = load_soul()
    if memory is None:
        return base
    block = memory.prompt_block()
    return f"{base}\n\n{block}" if block else base


def status_line(srs: SrsState, session: SessionState) -> str:
    """The one-line status strip: streak, due cards, current topic."""
    topic = session.current_topic or "—"
    return f" Streak {srs.streak.current} · {len(srs.due())} fällig · Thema: {topic} "


class Tui:
    """Moustache-banner terminal UI (TTY only).

    Layout: banner rows 1..n, transcript scrolls inside the terminal's
    scroll region (n+1..rows-1), status line pinned to the last row. Agent
    turns are marked with the moustache glyph `:-{)`. No full-screen input
    handling — the prompt lives at the bottom of the transcript, like the
    plain loop."""

    GLYPH = ":-{)"

    def __init__(self, write, rows: int = 24) -> None:
        self._write = write
        self.rows = max(rows, MIN_TUI_ROWS)
        self._status = ""

    def start(self) -> None:
        banner = (MOUSTACHE.strip("\n") + "\n" + TUI_TITLE + "\n" + TUI_COMMANDS).split("\n")
        self._banner_rows = len(banner)
        self._status_row = self.rows
        self._region_top = self._banner_rows + 1
        self._region_bottom = self.rows - 1
        frame = ["\x1b[2J\x1b[H", *banner, f"\x1b[{self._region_top};{self._region_bottom}r"]
        self._write("\n".join(frame))
        self._home_cursor()

    def agent_turn(self, text: str) -> None:
        self._home_cursor()
        self._write(f"{self.GLYPH} {text}\n")
        self.refresh_status()

    def note(self, text: str) -> None:
        self._home_cursor()
        self._write(f"{text}\n")
        self.refresh_status()

    def set_status(self, text: str) -> None:
        self._status = text
        self.refresh_status()

    def refresh_status(self) -> None:
        self._write(
            f"\x1b[{self._status_row};1H\x1b[2K{self._status}\x1b[{self._region_bottom};1H"
        )

    def stop(self) -> None:
        self._write(f"\x1b[r\x1b[{self.rows};1H")

    def _home_cursor(self) -> None:
        self._write(f"\x1b[{self._region_bottom};1H")


def _make_tui(output_fn, tty: bool | None) -> Tui | None:
    """Build the TUI when stdout is a real terminal (or `tty` forces it) and
    the terminal is tall enough for the frame; else None (plain loop)."""
    if tty if tty is not None else (sys.stdout.isatty() and sys.stdin.isatty()):
        try:
            rows = sys.stdout.get_terminal_size().lines if hasattr(sys.stdout, "get_terminal_size") else 24
        except (ValueError, OSError):
            rows = 24
        if rows >= MIN_TUI_ROWS:
            return Tui(output_fn, rows=rows)
    return None


def run_chat(
    cfg: Config | None = None,
    tutor: Tutor | None = None,
    input_fn=input,
    output_fn=print,
    tty: bool | None = None,
) -> int:
    cfg = cfg or load_config()
    srs = SrsState(cfg.srs_path)
    ensure_seeded(srs)  # seed list present on first run → /quiz works instantly
    session = SessionState.load(cfg.session_path)
    memory = MemoryState.load(cfg.memory_path)
    vault = None
    if cfg.vault_root:
        from herrclaw_bridge.vault import Vault

        vault = Vault(cfg.vault_root)
    else:
        output_fn("⚠︎ HERR_VAULT ist nicht gesetzt — Obsidian-Notizen sind deaktiviert.")
    if tutor is None:
        tutor = Tutor(make_client(cfg.base_url), model=cfg.model, system_prompt=system_prompt_with_memory(memory))
    bridge = BridgeClient(cfg.bridge_url) if cfg.bridge_url else None
    tracker = Tracker(vault=vault, srs=srs)
    ctx = ChatContext(srs=srs, session=session, tracker=tracker, bridge=bridge, memory=memory)

    startup_note = _topic_from_calendar(bridge, session, output_fn)
    tui = _make_tui(output_fn, tty)

    def agent_says(text: str) -> None:
        if tui is None:
            output_fn(f"Herr Claw: {text}")
        else:
            tui.agent_turn(text)

    if tui is None:
        output_fn(BANNER)
    else:
        tui.start()
        tui.set_status(status_line(srs, session))

    history: list[dict[str, str]] = []
    try:
        while True:
            try:
                raw = input_fn("Du: ")
            except (EOFError, KeyboardInterrupt):
                if tui is None:
                    output_fn("")
                break
            raw = raw.strip()
            if not raw:
                continue

            outcome = dispatch(raw, ctx)
            if isinstance(outcome, Direct):
                agent_says(outcome.text)
                if tui is not None:
                    tui.set_status(status_line(srs, session))
                continue
            if isinstance(outcome, ToLLM):
                user_message, system_note = outcome.user_message, outcome.system_note
            else:
                user_message, system_note = raw, None
            if system_note is None and startup_note:
                system_note, startup_note = startup_note, None

            history.append({"role": "user", "content": user_message})
            try:
                reply = tutor.reply(history[-MAX_HISTORY_MESSAGES:], system_note=system_note)
            except LLMError as exc:
                if tui is None:
                    output_fn(f"⚠︎ {exc}")
                else:
                    tui.note(f"⚠︎ {exc}")
                break
            history.append({"role": "assistant", "content": reply})
            note_user_exchange(ctx, user_message, reply)
            agent_says(reply)
            if tui is not None:
                tui.set_status(status_line(srs, session))
    finally:
        tracker.end_session(topic=session.current_topic)
        session.last_session_turns = tracker.session_turns
        session.last_session_end = datetime.now().isoformat(timespec="seconds")
        srs.save()
        session.save()
        if tui is not None:
            tui.stop()
    if tracker.session_turns:
        output_fn("Tschüss! Bis zum nächsten Mal. 👋")
    return 0


def _topic_from_calendar(bridge: BridgeClient | None, session: SessionState, output_fn=print) -> str | None:
    """P2 flourish: when no topic is set and the bridge is up, today's
    calendar suggests the practice topic. Silent degradation — the TUI must
    start instantly and identically when the bridge is down."""
    if bridge is None or session.current_topic:
        return None
    try:
        events = fetch_today_events(bridge)
    except BridgeError:
        return None
    topic = suggest_topic(events, now=datetime.now())
    if not topic:
        return None
    session.current_topic = topic
    output_fn(f"📅 Aus deinem Kalender: heute üben wir „{topic}“.")
    return (
        f"Lucas startet eine neue Session. Aus seinem Kalender heute ergibt sich das "
        f"Thema „{topic}“ — beginne damit: EIN kurzer Satz zum Thema + EINE einfache Frage an Lucas (A1-A2)."
    )
