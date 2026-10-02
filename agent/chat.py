"""`herr-claw chat` — the text frontend (P1 loop, v0.5 real TUI, P6 thin client).

On a TTY the loop runs as a small terminal UI: a fixed moustache-mascot
banner on top, the transcript scrolling between banner and a fixed status
line (terminal scroll region), agent turns marked with the moustache glyph
`:-{)` (SPEC §4.6 — the moustache is the product's face in every frontend).
Off-TTY (pipes, tests) it degrades to the plain read–reply loop.

Since P6 the loop holds NO brain and NO state: every user line goes through
agent.turns (sandbox brain via `nemoclaw exec` by default, local brain as
break-glass fallback) — rendering is all that happens here. The moustache
is the product's face in every frontend.
"""

from __future__ import annotations

import sys
from datetime import datetime

from .turns import TurnResult, make_brain

MIN_TUI_ROWS = 14  # below this the moustache frame would not fit — plain loop

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


def status_line(srs, session) -> str:
    """The one-line status strip (local-state variant, kept for the plain
    brain path and tests): streak, due cards, current topic."""
    topic = session.current_topic or "—"
    return f" Streak {srs.streak.current} · {len(srs.due())} fällig · Thema: {topic} "


def status_text(result: TurnResult) -> str:
    """The status strip from a brain result — the thin client's variant
    (the host has no state of its own)."""
    topic = result.topic or "—"
    return f" Streak {result.streak} · {result.due} fällig · Thema: {topic} "


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
    cfg=None,
    brain=None,
    input_fn=input,
    output_fn=print,
    tty: bool | None = None,
) -> int:
    """The P1 read–reply loop, now over the brain seam: each user line is
    one brain turn (sandbox by default); this process only renders. The
    brain owns history, state and the session — nothing is saved here."""
    from .llm import LLMError
    from .turns import TurnError

    brain = brain or make_brain(cfg, output_fn)
    tui = _make_tui(output_fn, tty)

    def agent_says(text: str) -> None:
        if tui is None:
            output_fn(f"Herr Claw: {text}")
        else:
            tui.agent_turn(text)

    def note(text: str) -> None:
        if tui is None:
            output_fn(text)
        else:
            tui.note(text)

    if tui is None:
        output_fn(BANNER)
    else:
        tui.start()
        tui.set_status(" Bereit ")

    turns = 0
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
            try:
                result = brain.turn(raw)
            except (TurnError, LLMError) as exc:
                note(f"⚠︎ {exc}")
                break
            turns += 1
            agent_says(result.reply)
            if tui is not None:
                tui.set_status(status_text(result))
    finally:
        brain.close()
        if tui is not None:
            tui.stop()
    if turns:
        output_fn("Tschüss! Bis zum nächsten Mal. 👋")
    return 0
