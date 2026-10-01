"""agent/scheduling.py — pure slot picking, topic derivation, /üben booking."""

import json
from datetime import datetime

import pytest

from agent.bridge import BridgeError
from agent.scheduling import (
    EVENT_TITLE,
    REMINDER_TITLE_FMT,
    book_topic_session,
    fetch_today_events,
    pick_free_slot,
    suggest_topic,
)

NOW = datetime(2026, 10, 2, 14, 0)  # Friday afternoon


def ev(title, start, end, all_day=False):
    return {"title": title, "start": start, "end": end, "all_day": all_day}


class FakeBridge:
    """Records bridge calls; canned freebusy; optional failure mode."""

    def __init__(self, events=None, fail=False):
        self.events = events or []
        self.fail = fail
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if self.fail:
            raise BridgeError("Bridge nicht erreichbar (http://127.0.0.1:8765/mcp)")
        if name == "calendar_freebusy":
            return json.dumps({"start": "x", "end": "x", "events": self.events}, ensure_ascii=False)
        if name == "reminders_add":
            return "Erinnerung angelegt"
        if name == "calendar_add":
            return "Termin angelegt"
        raise AssertionError(f"unexpected tool {name}")


class TestPickFreeSlot:
    def test_first_free_after_existing_events(self):
        events = [ev("Standup", "2026-10-02T14:00", "2026-10-02T14:30")]
        slot = pick_free_slot(events, NOW)
        assert slot == datetime(2026, 10, 2, 14, 30)

    def test_free_slot_before_event(self):
        events = [ev("Zahnarzt", "2026-10-02T18:00", "2026-10-02T19:00")]
        slot = pick_free_slot(events, NOW)
        assert slot == datetime(2026, 10, 2, 14, 15)

    def test_all_day_events_do_not_block(self):
        events = [ev(" Reisen (ganztägig)", "2026-10-02T00:00", "2026-10-02T23:59", all_day=True)]
        assert pick_free_slot(events, NOW) == datetime(2026, 10, 2, 14, 15)

    def test_respects_lead_time_and_grid(self):
        now = datetime(2026, 10, 2, 14, 3)
        assert pick_free_slot([], now) == datetime(2026, 10, 2, 14, 15)

    def test_morning_clamped_to_day_start(self):
        now = datetime(2026, 10, 2, 6, 30)
        assert pick_free_slot([], now) == datetime(2026, 10, 2, 8, 0)

    def test_full_day_returns_none(self):
        events = [ev("Blocker", "2026-10-02T08:00", "2026-10-02T22:00")]
        assert pick_free_slot(events, NOW) is None

    def test_evening_after_day_end_returns_none(self):
        now = datetime(2026, 10, 2, 23, 30)
        assert pick_free_slot([], now) is None

    def test_exact_boundary_is_free(self):
        # event ends exactly when the candidate starts → no overlap
        events = [ev("A", "2026-10-02T14:00", "2026-10-02T15:00")]
        assert pick_free_slot(events, NOW) == datetime(2026, 10, 2, 15, 0)


class TestSuggestTopic:
    def test_keyword_map_upcoming(self):
        events = [ev("Anmeldung Bürgeramt", "2026-10-02T16:00", "2026-10-02T16:30")]
        assert suggest_topic(events, NOW) == "Behörden und Termine"

    def test_upcoming_preferred_over_past(self):
        events = [
            ev("Bäckertermin", "2026-10-02T09:00", "2026-10-02T09:30"),
            ev("Gym Training", "2026-10-02T19:00", "2026-10-02T20:00"),
        ]
        assert suggest_topic(events, NOW) == "Sport"

    def test_earlier_today_falls_back(self):
        events = [ev("Supermarktgroßeinkauf", "2026-10-02T10:00", "2026-10-02T11:00")]
        assert suggest_topic(events, NOW) == "Einkaufen"

    def test_unknown_event_returns_none(self):
        events = [ev("Vorstandssitzung Q4", "2026-10-02T16:00", "2026-10-02T17:00")]
        assert suggest_topic(events, NOW) is None

    def test_empty_returns_none(self):
        assert suggest_topic([], NOW) is None

    def test_all_day_ignored_for_upcoming(self):
        events = [ev("Bäckerei-Tag", "2026-10-02T00:00", "2026-10-02T23:59", all_day=True)]
        assert suggest_topic(events, NOW) == "Beim Bäcker"


class TestBookTopicSession:
    def test_books_reminder_and_event(self):
        events = [ev("Meeting", "2026-10-02T14:00", "2026-10-02T14:30")]
        bridge = FakeBridge(events)
        summary = book_topic_session(bridge, "Bäckerei", now=NOW)
        names = [c[0] for c in bridge.calls]
        assert names == ["calendar_freebusy", "reminders_add", "calendar_add"]
        rem_args = bridge.calls[1][1]
        assert rem_args["title"] == REMINDER_TITLE_FMT.format(topic="Bäckerei")
        assert rem_args["due"] == "2026-10-02T14:30"
        cal_args = bridge.calls[2][1]
        assert cal_args["title"] == EVENT_TITLE
        assert cal_args["start"] == "2026-10-02T14:30"
        assert cal_args["duration_min"] == 15
        assert "14:30" in summary

    def test_full_day_books_reminder_for_evening(self):
        bridge = FakeBridge([ev("Blocker", "2026-10-02T08:00", "2026-10-02T22:00")])
        summary = book_topic_session(bridge, "Bäckerei", now=NOW)
        names = [c[0] for c in bridge.calls]
        assert "calendar_add" not in names
        assert bridge.calls[1][1]["due"] == "2026-10-02T20:00"
        assert "kein freies" in summary.lower()

    def test_bridge_down_degrades_honestly(self):
        bridge = FakeBridge(fail=True)
        summary = book_topic_session(bridge, "Bäckerei", now=NOW)
        assert "Keine Kalenderprüfung" in summary
        assert "Erinnerung fehlgeschlagen" in summary

    def test_calendar_failure_keeps_reminder(self):
        class HalfBroken(FakeBridge):
            def call_tool(self, name, arguments=None):
                if name == "calendar_add":
                    raise BridgeError("boom")
                return super().call_tool(name, arguments)

        bridge = HalfBroken([ev("Meeting", "2026-10-02T14:00", "2026-10-02T14:30")])
        summary = book_topic_session(bridge, "Bäckerei", now=NOW)
        assert "Erinnerung angelegt" in summary
        assert "Kalender fehlgeschlagen" in summary

    def test_fetch_today_events_uses_full_day(self):
        bridge = FakeBridge()
        events = fetch_today_events(bridge, now=NOW)
        assert events == []
        args = bridge.calls[0][1]
        assert args["start"] == "2026-10-02T00:00"
        assert args["end"] == "2026-10-03T00:00"
