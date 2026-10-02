"""The v0.5 chat TUI: moustache banner, `:-{)` agent turns, status line,
scroll-region frame — and the off-TTY plain-loop fallback (SPEC §4.6).
P6: the loop is a thin client; a FakeBrain stands in for the sandbox."""

import sys

from agent.chat import MIN_TUI_ROWS, Tui, run_chat, status_line
from agent.memory import MemoryState
from agent.turns import TurnError, TurnResult, load_soul, system_prompt_with_memory


class FakeBrain:
    def __init__(self, error: Exception | None = None):
        self.texts: list[str] = []
        self.error = error
        self.closed = False

    def turn(self, text: str) -> TurnResult:
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return TurnResult(
            reply=f"Antwort Nummer {len(self.texts)}. Wie geht es dir?",
            streak=len(self.texts),
            due=3,
            topic="Beim Bäcker",
        )

    def close(self) -> None:
        self.closed = True


def make_io(responses):
    iterator = iter(responses)
    outputs: list[str] = []

    def input_fn(prompt=""):
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError

    return input_fn, outputs.append, outputs


# ---- Tui frame -----------------------------------------------------------------


def test_tui_start_draws_moustache_banner_and_scroll_region():
    writes: list[str] = []
    tui = Tui(writes.append, rows=24)
    tui.start()
    frame = writes[0]
    assert frame.startswith("\x1b[2J\x1b[H")  # clear + home
    assert "o o" in frame and "\\___/" in frame  # the moustache mascot
    assert "Herr Claw" in frame
    # banner is 9 rows → transcript scrolls in 10..23, status pinned to 24
    assert "\x1b[10;23r" in frame


def test_tui_agent_turns_carry_the_moustache_glyph():
    writes: list[str] = []
    tui = Tui(writes.append, rows=24)
    tui.start()
    writes.clear()
    tui.agent_turn("Guten Tag!")
    assert ":-{) Guten Tag!" in "".join(writes)
    tui.set_status(" Streak 3 · 5 fällig · Thema: Bäckerei ")
    assert "\x1b[24;1H\x1b[2K" in "".join(writes)  # status rewritten on the last row
    assert writes[-1].endswith("\x1b[23;1H")  # cursor back inside the region


def test_tui_stop_resets_the_terminal():
    writes: list[str] = []
    Tui(writes.append, rows=24).stop()
    assert writes == ["\x1b[r\x1b[24;1H"]


def test_tui_rows_never_below_the_frame_minimum():
    writes: list[str] = []
    tui = Tui(writes.append, rows=4)
    assert tui.rows >= MIN_TUI_ROWS


# ---- run_chat: TTY mode vs plain fallback ----------------------------------------


def test_run_chat_renders_tui_when_tty(config):
    fake = FakeBrain()
    input_fn, output_fn, outputs = make_io(["Hallo!", "Noch was!"])
    code = run_chat(cfg=config, brain=fake, input_fn=input_fn, output_fn=output_fn, tty=True)
    assert code == 0
    combined = "\n".join(outputs)
    assert "\\___/" in combined  # moustache banner drawn
    assert ":-{) Antwort Nummer 1." in combined  # agent turns marked
    assert "Herr Claw: " not in combined  # the plain prefix is gone in TUI mode
    assert "Streak" in combined  # status strip visible (from the brain's result)
    assert "\x1b[r" in combined  # terminal restored on exit
    assert fake.texts == ["Hallo!", "Noch was!"]
    assert fake.closed  # the loop closes the brain (session end lives there)


def test_run_chat_plain_loop_off_tty(config):
    fake = FakeBrain()
    input_fn, output_fn, outputs = make_io(["Hallo!"])
    run_chat(cfg=config, brain=fake, input_fn=input_fn, output_fn=output_fn, tty=False)
    combined = "\n".join(outputs)
    assert "Herr Claw: Antwort Nummer 1." in combined
    assert "\\___/" not in combined  # no moustache frame off-TTY


def test_run_chat_falls_back_when_terminal_too_small(config, monkeypatch):
    class TinyStream:
        def isatty(self):
            return True

        def get_terminal_size(self):
            import os

            return os.terminal_size((80, 8))

    monkeypatch.setattr(sys, "stdout", TinyStream())
    monkeypatch.setattr(sys, "stdin", TinyStream())
    fake = FakeBrain()
    input_fn, output_fn, outputs = make_io(["Hallo!"])
    run_chat(cfg=config, brain=fake, input_fn=input_fn, output_fn=output_fn)  # tty=None → probe
    combined = "\n".join(outputs)
    assert "Herr Claw: Antwort Nummer 1." in combined  # degraded to the plain loop


def test_run_chat_brain_error_warns_restores_terminal_and_closes(config):
    fake = FakeBrain(error=TurnError("Sandbox nicht erreichbar"))
    input_fn, output_fn, outputs = make_io(["Hallo!"])
    code = run_chat(cfg=config, brain=fake, input_fn=input_fn, output_fn=output_fn, tty=True)
    assert code == 0
    combined = "\n".join(outputs)
    assert "Sandbox nicht erreichbar" in combined  # the warning reached the user
    assert fake.closed  # terminal + brain shut down cleanly
    assert "\x1b[r" in combined


# ---- memory in every content-generating prompt ------------------------------------


def test_system_prompt_carries_the_memory_journal(tmp_path):
    soul = load_soul()
    assert system_prompt_with_memory(None) == soul
    empty = MemoryState.load(tmp_path / "memory.md")
    assert system_prompt_with_memory(empty) == soul  # empty journal adds no noise

    memory = MemoryState.load(tmp_path / "memory.md")
    memory.record_interest("Bäckerei", landed=True)
    memory.record_success("Ich hätte gern zwei Brötchen", today="2026-10-02")
    prompt = system_prompt_with_memory(memory)
    assert prompt.startswith(soul)
    assert "Bäckerei" in prompt and "Brötchen" in prompt


def test_status_line_shows_streak_due_topic(srs, session):
    srs.touch_day()
    session.current_topic = "Beim Bäcker"
    line = status_line(srs, session)
    assert "Streak 1" in line and "fällig" in line and "Beim Bäcker" in line
