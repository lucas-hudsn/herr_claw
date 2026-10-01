"""Apple Calendar — EventKit (pyobjc) preferred, AppleScript fallback for adds.

SPEC §4.5: EventKit for free/busy (AppleScript free/busy is miserable —
deliberately NOT attempted), AppleScript fallback only when EventKit is
unusable. Writes go ONLY to the calendar 'Deutsch Lernen' (created on first
use) — the calendar name is not caller-visible over MCP, the scope IS this
module, same pattern as reminders.py. Every call is audited.

Free/busy output is JSON, not prose: the daily loop (/üben, 08:00 nudge)
consumes it programmatically to pick a slot — parsing German prose would be
the fragile path (agent/scheduling.py).

EKEventStore is thread-affine (Apple: create and use on ONE thread), so every
EventKit operation serializes on a module lock and builds its own store —
uvicorn worker threads differ between calls.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

from .applescript import qa, run_applescript
from .audit import DEFAULT_AUDIT_LOG, audit
from .dates import parse_local
from .errors import BridgeDenied, BridgeError

DEUTSCH_CALENDAR = "Deutsch Lernen"

_MIN_DURATION = 5
_MAX_DURATION = 240

_ek_lock = threading.Lock()


class EventKitUnavailable(BridgeError):
    """pyobjc/EventKit cannot be used on this machine (import or API missing)."""


def _eventkit():
    try:
        import EventKit
        from Foundation import NSDate

        return EventKit, NSDate
    except ImportError as exc:
        raise EventKitUnavailable(f"pyobjc/EventKit fehlt: {exc}")


def _request_access(store) -> None:
    """Block until EventKit access is granted. macOS 14+ full-access API;
    denied/restricted becomes BridgeError (the P0 TCC grant makes the prompt
    a no-op on the demo machine)."""
    EK, _ = _eventkit()
    status = EK.EKEventStore.authorizationStatusForEntityType_(EK.EKEntityTypeEvent)
    if status in (EK.EKAuthorizationStatusDenied, EK.EKAuthorizationStatusRestricted):
        raise BridgeError(
            "Kalenderzugriff verweigert (EventKit) — Systemeinstellungen › Datenschutz › Kalender."
        )
    if status == EK.EKAuthorizationStatusNotDetermined:
        done, result = threading.Event(), {}

        def handler(granted, error):
            result["granted"] = bool(granted)
            done.set()

        store.requestFullAccessToEventsWithCompletion_(handler)
        if not done.wait(timeout=60):
            raise BridgeError("EventKit-Rechteanfrage nach 60s nicht beantwortet.")
        if not result["granted"]:
            raise BridgeError(
                "Kalenderzugriff verweigert (EventKit) — Systemeinstellungen › Datenschutz › Kalender."
            )


def _ns_date(NSDate, when: datetime):
    return NSDate.dateWithTimeIntervalSince1970_(when.timestamp())


def _dt_from_ns(ns_date) -> datetime:
    return datetime.fromtimestamp(ns_date.timeIntervalSince1970())


def freebusy(start: str, end: str, log_path=DEFAULT_AUDIT_LOG) -> str:
    """Events overlapping [start, end] as JSON — all calendars, read-only."""
    start_dt, end_dt = parse_local(start), parse_local(end)
    if end_dt <= start_dt:
        raise BridgeError("Ende muss nach dem Anfang liegen.")
    audit(
        "calendar.freebusy",
        f"{start_dt:%Y-%m-%d %H:%M}…{end_dt:%Y-%m-%d %H:%M}",
        allowed=True,
        log_path=log_path,
    )
    try:
        return _freebusy_eventkit(start_dt, end_dt)
    except EventKitUnavailable as exc:
        raise BridgeError(
            f"Free/Busy braucht EventKit ({exc}) — AppleScript-Fallback bewusst nicht versucht (SPEC §4.5)."
        ) from exc


def _freebusy_eventkit(start_dt: datetime, end_dt: datetime) -> str:
    EK, NSDate = _eventkit()
    with _ek_lock:
        store = EK.EKEventStore.alloc().init()
        _request_access(store)
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            _ns_date(NSDate, start_dt), _ns_date(NSDate, end_dt), None
        )
        events = list(store.eventsMatchingPredicate_(predicate) or [])
    events.sort(key=lambda e: e.startDate().timeIntervalSince1970())
    payload = {
        "start": start_dt.isoformat(timespec="minutes"),
        "end": end_dt.isoformat(timespec="minutes"),
        "events": [
            {
                "title": e.title() or "",
                "start": _dt_from_ns(e.startDate()).isoformat(timespec="minutes"),
                "end": _dt_from_ns(e.endDate()).isoformat(timespec="minutes"),
                "all_day": bool(e.isAllDay()),
                "calendar": e.calendar().title() if e.calendar() else "",
            }
            for e in events
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def add_event(
    title: str,
    start: str,
    duration_min: int = 15,
    calendar_name: str = DEUTSCH_CALENDAR,
    log_path=DEFAULT_AUDIT_LOG,
) -> str:
    """Create an event — ONLY in the 'Deutsch Lernen' calendar; any other
    calendar name is a policy denial (never executed, audited as denied)."""
    title = (title or "").strip()
    calendar_name = (calendar_name or "").strip()
    target = f"{calendar_name}: {title} @ {start}"

    if not title:
        audit("calendar.add", target, allowed=False, reason="empty title", log_path=log_path)
        raise BridgeError("Titel des Termins fehlt.")
    if calendar_name != DEUTSCH_CALENDAR:
        audit(
            "calendar.add",
            target,
            allowed=False,
            reason=f"calendar '{calendar_name}' not in allowlist ('{DEUTSCH_CALENDAR}')",
            log_path=log_path,
        )
        raise BridgeDenied(
            f"Kalender '{calendar_name}' verweigert — nur '{DEUTSCH_CALENDAR}' ist erlaubt."
        )
    try:
        duration_min = int(duration_min)
    except (TypeError, ValueError):
        audit("calendar.add", target, allowed=False, reason="duration not an int", log_path=log_path)
        raise BridgeError("Dauer in Minuten (Zahl), bitte.")
    if not _MIN_DURATION <= duration_min <= _MAX_DURATION:
        audit(
            "calendar.add",
            target,
            allowed=False,
            reason=f"duration {duration_min}min outside {_MIN_DURATION}–{_MAX_DURATION}",
            log_path=log_path,
        )
        raise BridgeError(f"Dauer zwischen {_MIN_DURATION} und {_MAX_DURATION} Minuten, bitte.")
    start_dt = parse_local(start)
    end_dt = start_dt + timedelta(minutes=duration_min)
    audit("calendar.add", target, allowed=True, log_path=log_path)

    try:
        _add_eventkit(title, start_dt, end_dt)
        path = "EventKit"
    except EventKitUnavailable as exc:
        _add_applescript_event(title, start_dt, end_dt)
        path = f"AppleScript (EventKit unbrauchbar: {exc})"
    return f"Termin in „{DEUTSCH_CALENDAR}“ angelegt: {title} ({start_dt:%d.%m. %H:%M}–{end_dt:%H:%M}, via {path})"


def _add_eventkit(title: str, start_dt: datetime, end_dt: datetime) -> None:
    EK, NSDate = _eventkit()
    with _ek_lock:
        store = EK.EKEventStore.alloc().init()
        _request_access(store)
        calendar = _deutsch_calendar(store, EK)
        event = EK.EKEvent.eventWithEventStore_(store)
        event.setTitle_(title)
        event.setStartDate_(_ns_date(NSDate, start_dt))
        event.setEndDate_(_ns_date(NSDate, end_dt))
        event.setCalendar_(calendar)
        ok, error = store.saveEvent_span_commit_error_(event, EK.EKSpanThisEvent, True, None)
        if not ok:
            raise BridgeError(f"EventKit konnte den Termin nicht speichern: {error}")


def _deutsch_calendar(store, EK):
    """Find (or create) the 'Deutsch Lernen' calendar. Source preference:
    iCloud (CalDAV) if configured, else a local calendar."""
    existing = [
        c
        for c in (store.calendarsForEntityType_(EK.EKEntityTypeEvent) or [])
        if c.title() == DEUTSCH_CALENDAR
    ]
    if existing:
        return existing[0]
    sources = list(store.sources() or [])
    source = next(
        (s for s in sources if s.sourceType() == EK.EKSourceTypeCalDAV),
        next((s for s in sources if s.sourceType() == EK.EKSourceTypeLocal), None),
    )
    if source is None:
        raise BridgeError("Kein Kalender-Source gefunden (iCloud/local) — Kalender.app konfigurieren?")
    calendar = EK.EKCalendar.calendarWithEventStore_(store)  # direct init is rejected by EventKit
    calendar.setTitle_(DEUTSCH_CALENDAR)
    calendar.setSource_(source)
    ok, error = store.saveCalendar_commit_error_(calendar, True, None)
    if not ok:
        raise BridgeError(f"Kalender '{DEUTSCH_CALENDAR}' konnte nicht angelegt werden: {error}")
    return calendar


def _add_applescript_event(title: str, start_dt: datetime, end_dt: datetime) -> None:
    """AppleScript fallback (SPEC §4.5). Component-wise date assignment avoids
    locale-dependent `date "..."` parsing; day set last so Feb 30 can't overflow."""
    def apple_date(var: str, when: datetime) -> str:
        return (
            f"set {var} to current date\n"
            f"set year of {var} to {when.year}\n"
            f"set month of {var} to {when.month}\n"
            f"set day of {var} to {when.day}\n"
            f"set time of {var} to {when.hour * 3600 + when.minute * 60 + when.second}"
        )

    script = (
        apple_date("d1", start_dt)
        + "\n"
        + apple_date("d2", end_dt)
        + "\n"
        + "tell application \"Calendar\"\n"
        f'    if not (exists calendar "{qa(DEUTSCH_CALENDAR)}") then\n'
        f'        make new calendar with properties {{name:"{qa(DEUTSCH_CALENDAR)}"}}\n'
        "    end if\n"
        f"    tell calendar \"{qa(DEUTSCH_CALENDAR)}\"\n"
        f'        make new event with properties {{summary:"{qa(title)}", start date:d1, end date:d2}}\n'
        "    end tell\n"
        "end tell"
    )
    run_applescript(script)
