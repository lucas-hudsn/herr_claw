"""agent/daemon.py — the ONE scheduler: §4.1 jobs, dedup, catch-up, Telegram.

No network, no Apple: bridge and Telegram are fakes, the vault points at
tmp_path, time is injected via now_fn.
"""

import json
from dataclasses import replace
from datetime import date, datetime, time

import pytest

from agent.bridge import BridgeError
from agent.daemon import (
    DEFAULT_JOBS,
    Daemon,
    load_schedule,
    next_run,
)
from agent.state import SessionState, VocabEntry

NOW = datetime(2026, 10, 2, 9, 0)  # Friday, 09:00 — nudge is due


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("agent.daemon._time.sleep", lambda s: None)


class FakeTelegram:
    def __init__(self, updates=None, fail=False):
        self.sent = []
        self.polls = []
        self.updates = updates or []
        self.fail = fail

    def get_updates(self, offset, timeout_s=50):
        self.polls.append((offset, timeout_s))
        if self.fail:
            from agent.telegram import TelegramError

            raise TelegramError("Keine Verbindung zu api.telegram.org — Netzwerk prüfen.")
        return self.updates

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class FakeBridge:
    """Canned freebusy + recording, like test_scheduling."""

    def __init__(self, events=None, fail=False):
        self.events = events or []
        self.fail = fail
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if self.fail:
            raise BridgeError("Bridge nicht erreichbar")
        if name == "calendar_freebusy":
            return json.dumps({"events": self.events}, ensure_ascii=False)
        if name == "reminders_add":
            return "Erinnerung angelegt"
        if name == "reminders_complete":
            return "1 Erinnerung abgehakt. ✅"
        if name == "calendar_add":
            return "Termin angelegt"
        raise AssertionError(f"unexpected tool {name}")


class FakeTutor:
    def __init__(self, reply="Antwort. Wie geht es dir?"):
        self.calls = []
        self.reply_text = reply

    def reply(self, window, system_note=None):
        self.calls.append({"window": list(window), "system_note": system_note})
        return self.reply_text


def make_daemon(config, *, now=NOW, bridge=None, telegram=None, vault=None, tutor=None,
                jobs=None, session=None, memory=None, srs=None, rng=None):
    from agent.state import SrsState

    return Daemon(
        replace(config, bridge_url=None),
        srs=srs or SrsState(config.srs_path),
        session=session or SessionState.load(config.session_path),
        telegram=telegram or FakeTelegram(),
        chat_id="111",
        bridge=bridge,
        vault=vault,
        tutor=tutor or FakeTutor(),
        jobs=jobs or dict(DEFAULT_JOBS),
        memory=memory,
        rng=rng,
        now_fn=lambda: now,
    )


# ---- schedule loading ------------------------------------------------------------


def test_load_schedule_parses_yaml(tmp_path):
    path = tmp_path / "schedule.yaml"
    path.write_text('jobs:\n  nudge: "07:30"\n  quiz: "12:00"\n', encoding="utf-8")
    assert load_schedule(path) == {"nudge": time(7, 30), "quiz": time(12, 0)}


def test_load_schedule_falls_back_to_spec_times(tmp_path, capsys):
    assert load_schedule(tmp_path / "missing.yaml") == DEFAULT_JOBS
    broken = tmp_path / "broken.yaml"
    broken.write_text("jobs: [unclosed", encoding="utf-8")
    assert load_schedule(broken) == DEFAULT_JOBS  # never breaks the daemon
    assert load_schedule(broken, output_fn=lambda s: None) == DEFAULT_JOBS


def test_repo_schedule_yaml_has_the_spec_jobs():
    jobs = load_schedule()  # agent/schedule.yaml in the repo
    assert jobs == {"nudge": time(8, 0), "quiz": time(12, 30), "recap": time(20, 0)}


def test_next_run_today_or_tomorrow():
    assert next_run(time(10, 0), NOW) == datetime(2026, 10, 2, 10, 0)
    assert next_run(time(8, 0), NOW) == datetime(2026, 10, 3, 8, 0)


# ---- pending / dedup / catch-up ----------------------------------------------------


def test_pending_and_catchup(config):
    daemon = make_daemon(config, now=NOW)
    assert daemon.pending_jobs(NOW) == ["nudge"]
    assert daemon._run_job("nudge", NOW) is True
    assert daemon.pending_jobs(NOW) == []  # marked done — restarts won't re-fire

    session = SessionState.load(config.session_path)
    assert session.jobs_done["nudge"] == "2026-10-02"


def test_catch_up_runs_missed_jobs_in_order(config):
    evening = datetime(2026, 10, 2, 21, 0)  # started late — all three jobs missed
    daemon = make_daemon(config, now=evening, bridge=FakeBridge(), telegram=FakeTelegram())
    daemon.catch_up()
    telegram = daemon.telegram
    assert len(telegram.sent) == 3
    assert "Guten Morgen" in telegram.sent[0][1]
    assert "Mini-Quiz" in telegram.sent[1][1]
    assert "Tages-Recap" in telegram.sent[2][1]
    assert daemon.pending_jobs(evening) == []  # a second start today is a no-op


def test_failed_job_is_not_marked_done(config):
    daemon = make_daemon(config, now=NOW, bridge=FakeBridge(fail=True))

    def exploding_send(text):
        raise RuntimeError("no phone")

    daemon._send = exploding_send  # nudge fails at the send step
    assert daemon._run_job("nudge", NOW) is False
    assert daemon.session.jobs_done.get("nudge") != "2026-10-02"  # retried later


def test_paused_day_skips_jobs_without_marking_failure(config):
    daemon = make_daemon(config, now=NOW)
    daemon.session.pause_until = date(2026, 10, 5)
    assert daemon._run_job("nudge", NOW) is True
    assert daemon.session.jobs_done.get("nudge") == "2026-10-02"
    assert daemon.telegram.sent == []


# ---- 08:00 nudge ---------------------------------------------------------------------


def test_nudge_derives_topic_books_slot_and_pings_phone(config):
    events = [
        {"title": "Anmeldung Bürgeramt", "start": "2026-10-02T16:00", "end": "2026-10-02T16:30", "all_day": False}
    ]
    bridge = FakeBridge(events=events)
    daemon = make_daemon(config, now=NOW, bridge=bridge)
    daemon.job_nudge(NOW)

    text = daemon.telegram.sent[0][1]
    assert "Guten Morgen" in text and "Behörden und Termine" in text
    booking_calls = [name for name, _ in bridge.calls]
    assert booking_calls == ["calendar_freebusy", "reminders_add", "calendar_add"]
    reminder_args = bridge.calls[1][1]
    assert reminder_args["title"] == "Deutsch: 5-Min Chat — Thema Behörden und Termine"
    assert reminder_args["due"] == "2026-10-02T09:15"  # first grid slot ≥ now+lead
    assert bridge.calls[2][1]["title"] == "Deutsch Lernen (15m)"


def test_nudge_without_bridge_still_sends(config):
    daemon = make_daemon(config, now=NOW, bridge=None)
    daemon.job_nudge(NOW)
    text = daemon.telegram.sent[0][1]
    assert "Guten Morgen" in text and "Keine Bridge" in text


def test_nudge_degrades_honestly_when_bridge_down(config):
    daemon = make_daemon(config, now=NOW, bridge=FakeBridge(fail=True))
    daemon.job_nudge(NOW)
    text = daemon.telegram.sent[0][1]
    assert "Guten Morgen" in text  # the nudge still goes out
    assert "Keine Kalenderprüfung" in text or "fehlgeschlagen" in text


def test_nudge_falls_back_to_weakest_vocab_theme(config):
    daemon = make_daemon(config, now=NOW, bridge=FakeBridge(events=[]))
    from agent.state import SrsState

    srs = SrsState(config.srs_path)
    srs.vocab["der Zug"] = VocabEntry(en="train", level=1)  # theme: Berlin & Alltag
    daemon.srs = srs
    daemon.ctx.srs = srs
    daemon.job_nudge(NOW)
    assert "Berlin & Alltag" in daemon.telegram.sent[0][1]


# ---- 12:30 micro-quiz ------------------------------------------------------------------


def test_quiz_job_only_runs_when_nudge_ran(config):
    daemon = make_daemon(config, now=datetime(2026, 10, 2, 12, 30))
    daemon.job_quiz(daemon._now())
    assert daemon.telegram.sent == []  # morning nudge missing → no quiz

    daemon.session.jobs_done["nudge"] = "2026-10-02"
    daemon.job_quiz(daemon._now())
    assert len(daemon.telegram.sent) == 1
    assert "Mini-Quiz" in daemon.telegram.sent[0][1]
    assert "Frage 1/3" in daemon.telegram.sent[0][1]
    assert daemon.ctx.quiz is not None and len(daemon.ctx.quiz.questions) == 3


def test_quiz_job_does_not_stack_a_second_quiz(config):
    daemon = make_daemon(config, now=datetime(2026, 10, 2, 12, 30))
    daemon.session.jobs_done["nudge"] = "2026-10-02"
    daemon.job_quiz(daemon._now())
    active = daemon.ctx.quiz
    daemon.job_quiz(daemon._now())  # quiz still running → ignored
    assert daemon.ctx.quiz is active


# ---- 20:00 recap -------------------------------------------------------------------------


def test_recap_writes_daily_note_progress_and_completes_reminders(config, vault):
    bridge = FakeBridge()
    daemon = make_daemon(config, now=datetime(2026, 10, 2, 20, 0), bridge=bridge, vault=vault)
    daemon.srs.touch_day(date(2026, 10, 2))
    daemon.job_recap(daemon._now())

    assert ("reminders_complete", {"title": "5-Min Chat"}) in bridge.calls
    daily = vault.read("Daily notes/2026-10-02-deutsch.md")
    assert "Tages-Recap" in daily and "Streak: 1" in daily
    progress = vault.read("Deutsch/progress.md")
    assert "Tages-Recap" in progress
    assert "Tages-Recap gespeichert" in daemon.telegram.sent[0][1]


def test_recap_without_vault_still_confirms(config):
    daemon = make_daemon(config, now=datetime(2026, 10, 2, 20, 0), bridge=FakeBridge(), vault=None)
    daemon.job_recap(daemon._now())
    assert daemon.telegram.sent and "Tages-Recap" in daemon.telegram.sent[0][1]


def test_recap_bridge_down_still_writes_note(config, vault):
    daemon = make_daemon(config, now=datetime(2026, 10, 2, 20, 0), bridge=FakeBridge(fail=True), vault=vault)
    daemon.srs.touch_day(date(2026, 10, 2))
    daemon.job_recap(daemon._now())
    daily = vault.read("Daily notes/2026-10-02-deutsch.md")
    assert "nicht abgehakt" in daily  # honest degradation is visible in the note


# ---- Telegram turns -------------------------------------------------------------------------


def test_foreign_chats_are_never_answered(config):
    daemon = make_daemon(config)
    daemon.handle_update({"update_id": 1, "message": {"text": "Hallo", "chat": {"id": "999"}}})
    assert daemon.telegram.sent == []  # ignored — bot answers ONE chat


def test_command_from_the_phone_gets_a_direct_reply(config):
    daemon = make_daemon(config)
    daemon.handle_update({"update_id": 1, "message": {"text": "/fortschritt", "chat": {"id": "111"}}})
    assert "Fortschritt" in daemon.telegram.sent[0][1]
    assert daemon.telegram.sent[0][0] == "111"


def test_plain_text_goes_to_the_tutor_and_counts_practice(config):
    daemon = make_daemon(config, tutor=FakeTutor("Sehr gut! Richtig: Ich gehe / Deine Version: Ich gehe nicht."))
    before_streak = daemon.srs.streak.current
    daemon.handle_update({"update_id": 1, "message": {"text": "Ich gehe nicht zum Bäcker", "chat": {"id": "111"}}})
    assert daemon.tutor.calls and daemon.tutor.calls[0]["window"][0]["content"] == "Ich gehe nicht zum Bäcker"
    assert "Sehr gut!" in daemon.telegram.sent[0][1]
    assert daemon.srs.streak.current == before_streak + 1  # touch_day on a real exchange


def test_quiz_answered_from_the_phone(config):
    daemon = make_daemon(config, now=datetime(2026, 10, 2, 12, 30))
    daemon.session.jobs_done["nudge"] = "2026-10-02"
    daemon.job_quiz(daemon._now())
    question = daemon.ctx.quiz.questions[0]
    daemon.handle_update({"update_id": 2, "message": {"text": question.accepted[0], "chat": {"id": "111"}}})
    reply = daemon.telegram.sent[-1][1]
    assert "✅" in reply  # correct answer graded
    assert daemon.ctx.quiz.index == 1


def test_llm_failure_keeps_the_bot_alive(config):
    class ExplodingTutor:
        def reply(self, window, system_note=None):
            from agent.llm import LLMError

            raise LLMError("Netzwerk weg")

    daemon = make_daemon(config, tutor=ExplodingTutor())
    daemon.handle_update({"update_id": 1, "message": {"text": "Hallo", "chat": {"id": "111"}}})
    assert "Netzwerk weg" in daemon.telegram.sent[0][1]


# ---- v0.5: Morgen-Brief (08:00 nudge) -------------------------------------------------


def test_nudge_sends_the_morgen_brief_with_phrases(config, vault):
    from agent.phrases import PHRASE_BANK

    events = [
        {"title": "Anmeldung Bürgeramt", "start": "2026-10-02T16:00", "end": "2026-10-02T16:30", "all_day": False}
    ]
    daemon = make_daemon(config, now=NOW, bridge=FakeBridge(events=events), vault=vault)
    daemon.job_nudge(NOW)

    text = daemon.telegram.sent[0][1]
    assert "Guten Morgen" in text and "Behörden und Termine" in text
    assert "Deine Phrasen für heute:" in text
    assert "1. " in text
    phrases = [line.split(". ", 1)[1] for line in text.splitlines() if line[:2].strip(".").isdigit()]
    assert 5 <= len(phrases) <= 8  # SPEC §4.6: 5–8 phrases
    assert any(p in PHRASE_BANK["Behörden und Termine"] for p in phrases)  # event-tailored
    # the plan is stored so /tag re-shows exactly this all day
    plan = daemon.session.day_plan
    assert plan["date"] == "2026-10-02" and plan["source"] == "calendar"
    assert plan["phrases"] == phrases
    # the daily note carries the same brief
    daily = vault.read("Daily notes/2026-10-02-deutsch.md")
    assert "Morgen-Brief" in daily and "Behörden und Termine" in daily


def test_nudge_admits_when_the_calendar_was_unreadable(config):
    daemon = make_daemon(config, now=NOW, bridge=FakeBridge(fail=True))
    daemon.job_nudge(NOW)
    text = daemon.telegram.sent[0][1]
    assert "Kein Kalender gelesen" in text  # honest fallback (SPEC §4.6)
    assert daemon.session.day_plan["source"] == "fallback"


def test_nudge_lets_memory_interests_pick_the_flavor(config):
    from agent.memory import MemoryState

    from agent.phrases import PHRASE_BANK

    memory = MemoryState.load(config.memory_path)
    memory.record_interest("Bäckerei", landed=True)
    daemon = make_daemon(config, now=NOW, bridge=FakeBridge(events=[]), memory=memory)
    daemon.job_nudge(NOW)
    text = daemon.telegram.sent[0][1]
    assert any(phrase in text for phrase in PHRASE_BANK["Beim Bäcker"])  # flavor came from memory


def test_phone_report_records_success_and_reinforces_srs(config, vault):
    """20:00 Erfolgs-Check answered on the phone in plain text — the day-plan
    phrase lands in memory.md, SRS and Deutsch/progress.md."""
    from agent.memory import MemoryState
    from agent.quiz import ensure_seeded
    from agent.state import SrsState

    daemon_srs = SrsState(config.srs_path)
    ensure_seeded(daemon_srs)
    memory = MemoryState.load(config.memory_path)
    daemon = make_daemon(
        config, now=NOW, bridge=FakeBridge(), vault=vault, memory=memory, srs=daemon_srs
    )
    daemon.session.day_plan = {
        "date": NOW.date().isoformat(),
        "topic": "Beim Bäcker",
        "phrases": ["Ich hätte gern zwei Brötchen, bitte.", "Was kostet das Brot?"],
        "source": "calendar",
    }
    daemon.session.current_topic = "Beim Bäcker"
    daemon.respond("Guten Abend! Ich hätte gern zwei Brötchen bitte habe ich heute benutzt.")

    reloaded = MemoryState.load(config.memory_path)
    assert reloaded.erfolge and "Brötchen" in reloaded.erfolge[0].phrase
    assert reloaded.erfolge[0].context == "Beim Bäcker"
    assert daemon_srs.vocab["das Brötchen"].level == 2  # reinforced
    progress = vault.read("Deutsch/progress.md")
    assert "Erfolg" in progress


# ---- v0.5: Abend-Recap Erfolgs-Check ---------------------------------------------------


def test_recap_asks_which_phrases_were_used(config, vault):
    daemon = make_daemon(config, now=datetime(2026, 10, 2, 20, 0), bridge=FakeBridge(), vault=vault)
    daemon.session.day_plan = {
        "date": "2026-10-02",
        "topic": "Beim Bäcker",
        "phrases": ["a", "b", "c", "d", "e"],
        "source": "calendar",
    }
    daemon.job_recap(daemon._now())
    text = daemon.telegram.sent[0][1]
    assert "Tages-Recap gespeichert" in text
    assert "Abend-Check" in text and "/erfolge" in text
    assert "5 heutigen Phrasen" in text


def test_recap_without_plan_still_asks(config):
    daemon = make_daemon(config, now=datetime(2026, 10, 2, 20, 0), bridge=FakeBridge())
    daemon.job_recap(daemon._now())
    assert "Abend-Check" in daemon.telegram.sent[0][1]


# ---- poll / sync / run loop ---------------------------------------------------------------------


def test_poll_swallows_telegram_outages_and_keeps_offset(config):
    telegram = FakeTelegram(fail=True)
    daemon = make_daemon(config, telegram=telegram)
    assert daemon.poll(7, 10) == 7  # offset unchanged, no raise
    assert telegram.polls == [(7, 10)]


def test_poll_advances_offset_and_dispatches(config):
    telegram = FakeTelegram(
        updates=[{"update_id": 4, "message": {"text": "/fortschritt", "chat": {"id": "111"}}}]
    )
    daemon = make_daemon(config, telegram=telegram)
    assert daemon.poll(0, 10) == 5
    assert telegram.sent


def test_sync_updates_skips_backlog(config):
    daemon = make_daemon(config, telegram=FakeTelegram(updates=[{"update_id": 41}]))
    assert daemon.sync_updates() == 42
    daemon = make_daemon(config, telegram=FakeTelegram(updates=[]))
    assert daemon.sync_updates() == 0


def test_run_loop_stops_on_ctrl_c_and_saves(config, monkeypatch):
    daemon = make_daemon(config, now=datetime(2026, 10, 2, 3, 0))  # all jobs in the future
    calls = {"n": 0}

    def flaky_poll(offset, timeout_s):
        calls["n"] += 1
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "poll", flaky_poll)
    assert daemon.run() == 0
    assert calls["n"] == 1
    # state persisted cleanly
    assert SessionState.load(config.session_path) is not None


def test_run_loop_fires_due_jobs_immediately(config, monkeypatch):
    daemon = make_daemon(config, now=NOW, bridge=FakeBridge())

    def stop_after_first_poll(offset, timeout_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "poll", stop_after_first_poll)
    assert daemon.run() == 0
    assert "Guten Morgen" in daemon.telegram.sent[0][1]  # catch-up ran before polling
    assert daemon.session.jobs_done.get("nudge") == "2026-10-02"
