"""German slash commands — stable names, state-only behavior."""

from agent.commands import ChatContext, Direct, ToLLM, dispatch, COMMANDS
from agent.tracker import Tracker


def make_ctx(srs, session) -> ChatContext:
    return ChatContext(srs=srs, session=session, tracker=Tracker(vault=None, srs=srs))


def test_non_command_passes_through(srs, session):
    assert dispatch("Guten Tag!", make_ctx(srs, session)) is None


def test_fortschritt_shows_streak_and_totals(srs, session):
    ctx = make_ctx(srs, session)
    srs.touch_day()
    srs.add_message()
    srs.add_mistake("ich bin ging", "ich ging")
    out = dispatch("/fortschritt", ctx)
    assert isinstance(out, Direct)
    assert "Streak: 1" in out.text
    assert "1 Nachrichten" in out.text and "1 Korrekturen" in out.text


def test_fehler_lists_and_praises_empty(srs, session):
    ctx = make_ctx(srs, session)
    out = dispatch("/fehler", ctx)
    assert "noch keine Fehler" in out.text
    srs.add_mistake("ich bin ging", "ich ging")
    srs.add_mistake("ich bin ging", "ich ging")
    out = dispatch("/fehler", ctx)
    assert "„ich bin ging“ → „ich ging“ (2×)" in out.text


def test_pause_sets_date_and_validates(srs, session):
    ctx = make_ctx(srs, session)
    out = dispatch("/pause 3", ctx)
    assert isinstance(out, Direct)
    assert session.pause_until is not None
    out = dispatch("/pause später", ctx)
    assert "Wie viele Tage" in out.text
    out = dispatch("/pause 9999", ctx)
    assert "1 und 365" in out.text
    assert session.pause_until is not None  # unchanged by invalid input


def test_pause_defaults_to_one_day(srs, session):
    dispatch("/pause", make_ctx(srs, session))
    assert session.pause_until is not None


def test_quiz_is_honest_stub(srs, session):
    out = dispatch("/quiz", make_ctx(srs, session))
    assert isinstance(out, Direct)
    assert "Phase 4" in out.text


def test_sprechen_points_to_phase3(srs, session):
    out = dispatch("/sprechen", make_ctx(srs, session))
    assert "Phase 3" in out.text


def test_erklaere_routes_to_llm_with_english_note(srs, session):
    out = dispatch("/erkläre umziehen", make_ctx(srs, session))
    assert isinstance(out, ToLLM)
    assert "umziehen" in out.user_message
    assert "ENGLISCH" in out.system_note


def test_erklaere_without_arg_asks(srs, session):
    out = dispatch("/erkläre", make_ctx(srs, session))
    assert isinstance(out, Direct) and "Was soll ich erklären" in out.text


def test_ueben_sets_topic_and_routes(srs, session):
    ctx = make_ctx(srs, session)
    out = dispatch("/üben Bäckerei", ctx)
    assert isinstance(out, ToLLM)
    assert "Bäckerei" in out.user_message
    assert session.current_topic == "Bäckerei"


def test_ueben_without_arg_asks_for_topic(srs, session):
    out = dispatch("/üben", make_ctx(srs, session))
    assert isinstance(out, Direct) and "Bäckerei" in out.text


def test_unknown_command_lists_commands(srs, session):
    out = dispatch("/blabla", make_ctx(srs, session))
    assert isinstance(out, Direct)
    for cmd in COMMANDS:
        assert cmd in out.text


def test_command_matching_is_case_insensitive(srs, session):
    out = dispatch("/FORTSCHRITT", make_ctx(srs, session))
    assert isinstance(out, Direct) and "Streak" in out.text
