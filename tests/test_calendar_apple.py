"""calendar_apple.py — EventKit path (faked), AppleScript fallback, allowlist."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from herrclaw_bridge import calendar_apple as cal
from herrclaw_bridge.errors import BridgeDenied, BridgeError


# ---- fake EventKit (pyobjc-shaped enough for this module) ------------------

class FakeNSDate:
    def __init__(self, ts):
        self.ts = ts

    @classmethod
    def dateWithTimeIntervalSince1970_(cls, ts):
        return cls(ts)

    def timeIntervalSince1970(self):
        return self.ts


class FakeCalendar:
    def __init__(self, title):
        self._title = title

    def title(self):
        return self._title


class FakeSource:
    def __init__(self, kind):
        self._kind = kind

    def sourceType(self):
        return self._kind


class FakeSavedEvent:
    def __init__(self, store):
        self._store = store
        self.title_value = ""
        self.start_ts = None
        self.end_ts = None
        self.calendar_value = None
        self.all_day = False

    def setTitle_(self, t):
        self.title_value = t

    def setStartDate_(self, d):
        self.start_ts = d.ts

    def setEndDate_(self, d):
        self.end_ts = d.ts

    def setCalendar_(self, c):
        self.calendar_value = c

    def startDate(self):
        return FakeNSDate(self.start_ts)

    def endDate(self):
        return FakeNSDate(self.end_ts)

    def title(self):
        return self.title_value

    def calendar(self):
        return self.calendar_value

    def isAllDay(self):
        return self.all_day


class FakeEKCalendar:
    def __init__(self, store=None):
        self._store = store
        self._title = ""
        self._source = None

    @classmethod
    def calendarWithEventStore_(cls, store):
        return cls(store)

    def setTitle_(self, title):
        self._title = title

    def setSource_(self, source):
        self._source = source

    def title(self):
        return self._title

    def sourceType(self):
        return self._source._kind


class FakeStore:
    AUTHORIZED = 3

    def __init__(self):
        self.events = []
        self.saved = []
        self.calendars = [FakeCalendar("Home"), FakeCalendar("Deutsch Lernen")]
        self.saved_calendars = []

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    @classmethod
    def authorizationStatusForEntityType_(cls, entity):
        return 3  # authorized — no prompt path in tests

    def predicateForEventsWithStartDate_endDate_calendars_(self, start, end, cals):
        return (start.ts, end.ts)

    def eventsMatchingPredicate_(self, pred):
        lo, hi = pred
        return [
            e for e in self.events
            if e.start_ts < hi and e.end_ts > lo
        ]

    def calendarsForEntityType_(self, entity):
        return self.calendars

    def sources(self):
        return [FakeSource(99), FakeSource(1)]  # 99=unknown, 1=CalDAV

    def saveCalendar_commit_error_(self, calendar, commit, error):
        self.saved_calendars.append(calendar)
        return True, None

    def saveEvent_span_commit_error_(self, event, span, commit, error):
        self.saved.append(event)
        return True, None


class FakeEventKit(SimpleNamespace):
    pass


def make_fake_ek(store=None):
    ek = FakeEventKit(
        EKEntityTypeEvent=0,
        EKSpanThisEvent=0,
        EKSourceTypeCalDAV=1,
        EKSourceTypeLocal=0,
        EKAuthorizationStatusDenied=2,
        EKAuthorizationStatusRestricted=1,
        EKAuthorizationStatusNotDetermined=0,
        EKEventStore=(store if store is not None else FakeStore),
        EKEvent=SimpleNamespace(eventWithEventStore_=lambda s: FakeSavedEvent(s)),
        EKCalendar=FakeEKCalendar,
    )
    return ek


@pytest.fixture
def ek_env(monkeypatch):
    store = FakeStore()

    class SharedStore(FakeStore):
        """EKEventStore.alloc().init() in the module always returns `store` —
        fresh stores would silently drop the events the test registered."""

        @classmethod
        def alloc(cls):
            return store

    fake = make_fake_ek(SharedStore)
    monkeypatch.setattr(cal, "_eventkit", lambda: (fake, FakeNSDate))
    return store


def _event(title, start_h, dur_h=1.0, all_day=False, calendar_title="Home", day=None):
    day = day or datetime(2026, 10, 2)
    start = day.replace(hour=int(start_h), minute=round((start_h % 1) * 60))
    ev = FakeSavedEvent(None)
    ev.title_value = title
    ev.start_ts = start.timestamp()
    ev.end_ts = start.timestamp() + dur_h * 3600
    ev.calendar_value = FakeCalendar(calendar_title)
    ev.all_day = all_day
    return ev


# ---- freebusy ---------------------------------------------------------------

def test_freebusy_json_sorted(ek_env, tmp_path):
    ek_env.events = [
        _event("Später", 16.0),
        _event("Früh", 9.0),
    ]
    out = cal.freebusy("2026-10-02 00:00", "2026-10-03 00:00", log_path=tmp_path / "a.log")
    payload = json.loads(out)
    titles = [e["title"] for e in payload["events"]]
    assert titles == ["Früh", "Später"]
    assert payload["events"][0]["calendar"] == "Home"


def test_freebusy_empty_window(ek_env, tmp_path):
    out = cal.freebusy("2026-10-02", "2026-10-03", log_path=tmp_path / "a.log")
    assert json.loads(out)["events"] == []


def test_freebusy_bad_range(ek_env, tmp_path):
    with pytest.raises(BridgeError):
        cal.freebusy("2026-10-03", "2026-10-02", log_path=None)


def test_freebusy_without_eventkit_fails_honestly(monkeypatch, tmp_path):
    def broken():
        raise cal.EventKitUnavailable("no pyobjc")

    monkeypatch.setattr(cal, "_eventkit", broken)
    with pytest.raises(BridgeError, match="EventKit"):
        cal.freebusy("2026-10-02", "2026-10-03", log_path=None)


# ---- add_event: allowlist ----------------------------------------------------

def test_add_denies_foreign_calendar(ek_env, tmp_path):
    with pytest.raises(BridgeDenied, match="Deutsch Lernen"):
        cal.add_event("Geheim", "2026-10-02 14:00", calendar_name="Family", log_path=tmp_path / "a.log")
    assert ek_env.saved == []
    entry = json.loads((tmp_path / "a.log").read_text().strip())
    assert entry["allowed"] is False and "not in allowlist" in entry["reason"]


def test_add_denies_bad_duration(ek_env, tmp_path):
    with pytest.raises(BridgeError):
        cal.add_event("X", "2026-10-02 14:00", duration_min=600, log_path=tmp_path / "a.log")
    assert ek_env.saved == []


def test_add_denies_empty_title(ek_env, tmp_path):
    with pytest.raises(BridgeError):
        cal.add_event("", "2026-10-02 14:00", log_path=tmp_path / "a.log")


def test_add_uses_deutsch_calendar(ek_env, tmp_path):
    out = cal.add_event("Deutsch Lernen (15m)", "2026-10-02 14:30", duration_min=15, log_path=tmp_path / "a.log")
    assert len(ek_env.saved) == 1
    ev = ek_env.saved[0]
    assert ev.title_value == "Deutsch Lernen (15m)"
    assert ev.calendar_value.title() == "Deutsch Lernen"
    assert datetime.fromtimestamp(ev.start_ts) == datetime(2026, 10, 2, 14, 30)
    assert datetime.fromtimestamp(ev.end_ts) == datetime(2026, 10, 2, 14, 45)
    assert "EventKit" in out


def test_add_creates_missing_calendar_in_icloud_source(ek_env, tmp_path):
    ek_env.calendars = [FakeCalendar("Home")]
    cal.add_event("X", "2026-10-02 14:00", log_path=tmp_path / "a.log")
    assert len(ek_env.saved_calendars) == 1
    assert ek_env.saved_calendars[0].title() == "Deutsch Lernen"
    assert ek_env.saved[0].calendar_value.sourceType() == 1  # CalDAV source preferred


def test_add_audits_allow(ek_env, tmp_path):
    cal.add_event("Deutsch Lernen (15m)", "2026-10-02 14:30", log_path=tmp_path / "a.log")
    entry = json.loads((tmp_path / "a.log").read_text().strip())
    assert entry["action"] == "calendar.add" and entry["allowed"] is True


# ---- add_event: AppleScript fallback ----------------------------------------

def test_add_falls_back_to_applescript(monkeypatch, tmp_path):
    def broken():
        raise cal.EventKitUnavailable("no pyobjc")

    monkeypatch.setattr(cal, "_eventkit", broken)
    ran = []
    monkeypatch.setattr(cal, "run_applescript", lambda s: ran.append(s))
    out = cal.add_event("Brötchen holen", "2026-10-02 08:00", duration_min=15, log_path=tmp_path / "a.log")
    script = ran[0]
    assert 'exists calendar "Deutsch Lernen"' in script
    assert 'summary:"Brötchen holen"' in script
    assert "set year of d1 to 2026" in script
    assert "AppleScript" in out


# ---- EventKit denied surface --------------------------------------------------

def test_access_denied_raises(monkeypatch, tmp_path):
    class DeniedStore(FakeStore):
        @classmethod
        def authorizationStatusForEntityType_(cls, entity):
            return 2  # denied

    class SharedDenied(DeniedStore):
        @classmethod
        def alloc(cls):
            return denied

    denied = DeniedStore()
    monkeypatch.setattr(cal, "_eventkit", lambda: (make_fake_ek(SharedDenied), FakeNSDate))
    with pytest.raises(BridgeError, match="Datenschutz"):
        cal.freebusy("2026-10-02", "2026-10-03", log_path=tmp_path / "a.log")
