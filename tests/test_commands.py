"""German slash commands — stable names, state-only behavior."""

from agent.commands import (
    ChatContext,
    Direct,
    ToLLM,
    dispatch,
    note_user_exchange,
    COMMANDS,
)
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


def test_quiz_starts_and_grades_answers_p4(srs, session):
    ctx = make_ctx(srs, session)
    out = dispatch("/quiz 2", ctx)
    assert isinstance(out, Direct)
    assert "Frage 1/2" in out.text
    assert ctx.quiz is not None and len(ctx.quiz.questions) == 2

    # the seed list is the only quiz content (anti-hallucination)
    assert all(q.key in srs.vocab for q in ctx.quiz.questions)

    # plain text is graded while the quiz is active; SRS applies the review
    first = ctx.quiz.questions[0]
    out = dispatch(first.accepted[0], ctx)
    assert isinstance(out, Direct) and "✅" in out.text
    assert srs.vocab[first.key].level == 2

    # second answer wrong → display shows article + plural, miss counted
    second = ctx.quiz.questions[1]
    out = dispatch("garbage nonsense", ctx)
    assert "❌ Leider falsch" in out.text and second.display in out.text
    assert srs.vocab[second.key].misses == 1
    assert ctx.quiz is None  # finished → plain text flows to the tutor again
    assert dispatch("Guten Tag!", ctx) is None


def test_quiz_quit_word_ends_early(srs, session):
    ctx = make_ctx(srs, session)
    dispatch("/quiz 5", ctx)
    out = dispatch("ende", ctx)
    assert "Quiz beendet" in out.text


def test_quiz_no_double_start_and_arg_validation(srs, session):
    ctx = make_ctx(srs, session)
    dispatch("/quiz", ctx)
    out = dispatch("/quiz 3", ctx)
    assert "läuft schon" in out.text

    fresh = make_ctx(srs, session)
    assert "Wie viele Fragen" in dispatch("/quiz später", fresh).text
    assert "1 und 20" in dispatch("/quiz 99", fresh).text
    assert fresh.quiz is None


def test_sprechen_points_to_the_terminal(srs, session):
    out = dispatch("/sprechen", make_ctx(srs, session))
    assert isinstance(out, Direct)
    assert "herr-claw sprechen" in out.text


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


class FakeBookingBridge:
    """Enough bridge for /üben: canned freebusy, records tool names."""

    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append(name)
        if name == "calendar_freebusy":
            return '{"events": []}'
        return "ok"


def test_ueben_books_via_bridge(srs, session):
    ctx = make_ctx(srs, session)
    ctx.bridge = FakeBookingBridge()
    out = dispatch("/üben Bäckerei", ctx)
    assert isinstance(out, ToLLM)
    assert session.current_topic == "Bäckerei"
    assert ctx.bridge.calls == ["calendar_freebusy", "reminders_add", "calendar_add"]
    assert "Buchung über die Bridge" in out.system_note
    assert "Bäckerei" in out.system_note


def test_ueben_without_bridge_still_sets_topic(srs, session):
    ctx = make_ctx(srs, session)  # bridge None — pre-bridge behavior unchanged
    out = dispatch("/üben Bäckerei", ctx)
    assert isinstance(out, ToLLM)
    assert "Buchung" not in out.system_note
    assert session.current_topic == "Bäckerei"


# ---- v0.5: /tag — today's tailored phrase plan ---------------------------------


def test_tag_lazily_builds_and_stores_the_plan(srs, session, tmp_path):
    from agent.memory import MemoryState

    ctx = make_ctx(srs, session)
    ctx.memory = MemoryState.load(tmp_path / "memory.md")
    out = dispatch("/tag", ctx)
    assert isinstance(out, Direct)
    assert "📋 Dein Tag" in out.text and "1. " in out.text
    assert 5 <= len(ctx.session.day_plan["phrases"]) <= 8  # stored for the whole day
    assert dispatch("/tag", ctx).text == out.text  # same plan re-shown, not rebuilt


def test_tag_re_shows_the_morning_plan_unchanged(srs, session, tmp_path):
    from agent.memory import MemoryState

    ctx = make_ctx(srs, session)
    ctx.memory = MemoryState.load(tmp_path / "memory.md")
    ctx.session.day_plan = {
        "date": session_day(),
        "topic": "Behörden und Termine",
        "phrases": ["Ich habe einen Termin beim Bürgeramt.", "Und noch einer."],
        "source": "calendar",
    }
    out = dispatch("/tag", ctx)
    assert "Ich habe einen Termin beim Bürgeramt." in out.text
    assert "aus deinem Kalender" in out.text  # the source is named honestly


def session_day():
    from datetime import date

    return date.today().isoformat()


# ---- v0.5: /erfolge — report a phrase you used ---------------------------------


def test_erfolge_empty_prompts_for_a_report(srs, session):
    out = dispatch("/erfolge", make_ctx(srs, session))
    assert isinstance(out, Direct)
    assert "/erfolge <Satz>" in out.text


def test_erfolge_with_arg_routes_to_llm_and_marks_success(srs, session):
    out = dispatch("/erfolge Ich habe einen Termin beim Bürgeramt.", make_ctx(srs, session))
    assert isinstance(out, ToLLM)
    assert "Ich habe einen Termin beim Bürgeramt." in out.user_message
    assert "Richtig: … / Deine Version: …" in out.system_note
    assert out.success_phrases == ("Ich habe einen Termin beim Bürgeramt.",)


def test_erfolge_reports_land_in_memory_srs_and_progress(srs, session, vault, tmp_path):
    from agent.memory import MemoryState
    from agent.quiz import ensure_seeded

    ensure_seeded(srs)
    ctx = make_ctx(srs, session)
    ctx.tracker = Tracker(vault=vault, srs=srs)
    ctx.memory = MemoryState.load(tmp_path / "memory.md")
    ctx.session.current_topic = "Beim Bäcker"
    srs.vocab["das Brötchen"].level = 1
    outcome = dispatch("/erfolge Ich hätte gern zwei Brötchen, bitte.", ctx)
    note_user_exchange(ctx, outcome.user_message, "Sehr gut!", outcome.success_phrases)

    # SRS reinforcement: exactly the seed word the phrase contains
    assert srs.vocab["das Brötchen"].level == 2
    assert srs.vocab["das Brot"].level == 1  # Brötchen ≠ Brot — untouched
    # memory journal, persisted
    reloaded = MemoryState.load(tmp_path / "memory.md")
    assert [s.phrase for s in reloaded.erfolge] == ["Ich hätte gern zwei Brötchen, bitte."]
    assert reloaded.erfolge[0].context == "Beim Bäcker"
    assert [i.topic for i in reloaded.interessen] == ["Beim Bäcker"]
    # append-only vault mirror
    progress = vault.read("Deutsch/progress.md")
    assert "Erfolg" in progress and "Brötchen" in progress


def test_note_user_exchange_records_day_plan_phrases_from_conversation(srs, session, vault, tmp_path):
    """The 20:00 Erfolgs-Check answered in plain text: a day-plan phrase
    mentioned in the reply is recorded — no slash command needed."""
    from agent.memory import MemoryState
    from agent.quiz import ensure_seeded

    ensure_seeded(srs)
    ctx = make_ctx(srs, session)
    ctx.tracker = Tracker(vault=vault, srs=srs)
    ctx.memory = MemoryState.load(tmp_path / "memory.md")
    ctx.session.day_plan = {
        "date": session_day(),
        "topic": "Beim Bäcker",
        "phrases": ["Ich hätte gern zwei Brötchen, bitte.", "Was kostet das Brot?"],
        "source": "calendar",
    }
    note_user_exchange(
        ctx,
        "Heute war ich beim Bäcker! Ich hätte gern zwei Brötchen bitte hat super funktioniert.",
        "Sehr gut! Richtig: Ich hätte gern zwei Brötchen, bitte. / Deine Version: zwei Brötchen bitte",
        (),
    )
    recorded = [s.phrase for s in ctx.memory.erfolge]
    assert "Ich hätte gern zwei Brötchen, bitte." in recorded
    assert "Was kostet das Brot?" not in recorded  # not mentioned → not recorded
    assert [i.topic for i in ctx.memory.interessen] == ["Beim Bäcker"]  # context landed
    # the correction in the reply is still logged as usual
    assert srs.mistakes and "Richtig" not in srs.mistakes[0].example
    # vault mirror
    progress = vault.read("Deutsch/progress.md")
    assert "Erfolg" in progress and "Brötchen" in progress
