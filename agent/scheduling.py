"""Agent-side scheduling logic on top of the bridge.

Pure functions (pick_free_slot, suggest_topic) work on plain dicts so the
tests never touch Apple. book_topic_session orchestrates the P2 checklist
flow: freebusy → pick a free 15-min slot → Reminder in 'Deutsch' + event in
'Deutsch Lernen' (SPEC §4.1 reminder/event wording). Every bridge failure
degrades to an honest note — practice never depends on the calendar.

The reminder title format is the daily-loop one (§4.1): "Deutsch: 5-Min
Chat — Thema [X]".
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta

from .bridge import BridgeClient, BridgeError

DAY_START = time(8, 0)
DAY_END = time(22, 0)
SLOT_MIN = 15
GRID_MIN = 15  # candidates align to quarter-hours — calendar-appropriate
LEAD_MIN = 5  # never book a slot starting in <5 minutes

REMINDER_TITLE_FMT = "Deutsch: 5-Min Chat — Thema {topic}"
EVENT_TITLE = "Deutsch Lernen (15m)"

# Calendar title (lowercase substring) → A1-practice topic. The flourish:
# today's events drive today's topic (SPEC P2). Ordered — first hit wins.
TOPIC_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("anmeldung", "Behörden und Termine"),
    ("bürgeramt", "Behörden und Termine"),
    ("behörde", "Behörden und Termine"),
    ("bäcker", "Beim Bäcker"),
    ("supermarkt", "Einkaufen"),
    ("einkauf", "Einkaufen"),
    ("rewe", "Einkaufen"),
    ("edeka", "Einkaufen"),
    ("arzt", "Beim Arzt"),
    ("zahnarzt", "Beim Arzt"),
    ("flug", "Reisen"),
    ("bahn", "Reisen"),
    ("reise", "Reisen"),
    ("urlaub", "Reisen"),
    ("gym", "Sport"),
    ("fitness", "Sport"),
    ("sport", "Sport"),
    ("yoga", "Sport"),
    ("restaurant", "Essen gehen"),
    ("café", "Essen gehen"),
    ("cafe", "Essen gehen"),
    ("meeting", "Arbeit und Büro"),
    ("standup", "Arbeit und Büro"),
    ("büro", "Arbeit und Büro"),
    ("office", "Arbeit und Büro"),
    ("wohnung", "Wohnung"),
    ("miete", "Wohnung"),
    ("umzug", "Wohnung"),
    ("einzug", "Wohnung"),
    ("besichtigung", "Wohnung"),
)


def parse_freebusy(json_text: str) -> list[dict]:
    """Extract the event dicts from a calendar_freebusy JSON payload."""
    return list(json.loads(json_text).get("events", []))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _ceil_to_grid(when: datetime) -> datetime:
    minute = (when.minute // GRID_MIN + (1 if when.minute % GRID_MIN else 0)) * GRID_MIN
    return when.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)


def pick_free_slot(
    events: list[dict],
    now: datetime,
    duration_min: int = SLOT_MIN,
) -> datetime | None:
    """First free quarter-hour-aligned slot ≥ now+LEAD, inside DAY_START–DAY_END
    today, not overlapping any timed event. All-day events don't block (they
    are rarely hour-precise). None when the day is full or already over."""
    day_open = datetime.combine(now.date(), DAY_START)
    day_close = datetime.combine(now.date(), DAY_END)
    earliest = _ceil_to_grid(now + timedelta(minutes=LEAD_MIN))
    candidate = max(earliest, day_open)
    busy = [
        (_dt(e["start"]), _dt(e["end"]))
        for e in events
        if not e.get("all_day") and e.get("start") and e.get("end")
    ]
    while candidate + timedelta(minutes=duration_min) <= day_close:
        end = candidate + timedelta(minutes=duration_min)
        if all(not (candidate < b_end and end > b_start) for b_start, b_end in busy):
            return candidate
        candidate += timedelta(minutes=GRID_MIN)
    return None


def suggest_topic(events: list[dict], now: datetime) -> str | None:
    """Topic from today's calendar titles: keyword match on upcoming events
    first, then anything earlier today. None when nothing maps — the tutor
    never invents a calendar connection that isn't there."""
    def topic_for(title: str) -> str | None:
        lowered = title.lower()
        return next((t for kw, t in TOPIC_KEYWORDS if kw in lowered), None)

    upcoming = sorted(
        (e for e in events if e.get("title") and not e.get("all_day") and _dt(e["start"]) >= now),
        key=lambda e: _dt(e["start"]),
    )
    for event in upcoming:
        topic = topic_for(event["title"])
        if topic:
            return topic
    for event in sorted(events, key=lambda e: _dt(e.get("start", now.isoformat()))):
        if event.get("title") and topic_for(event["title"]):
            return topic_for(event["title"])
    return None


def fetch_today_events(bridge: BridgeClient, now: datetime | None = None) -> list[dict]:
    """Today 00:00–24:00 free/busy via the bridge. Raises BridgeError when
    the bridge is down — callers decide how to degrade."""
    now = now or datetime.now()
    day = now.date()
    payload = bridge.call_tool(
        "calendar_freebusy",
        {
            "start": datetime.combine(day, time.min).isoformat(timespec="minutes"),
            "end": datetime.combine(day + timedelta(days=1), time.min).isoformat(timespec="minutes"),
        },
    )
    return parse_freebusy(payload)


def book_topic_session(bridge: BridgeClient, topic: str, now: datetime | None = None) -> str:
    """Book the /üben session: Reminder always, Calendar event when a slot is
    free. Returns a German one-line status (the honest-failure variant keeps
    the chat going when the bridge is down or a piece fails)."""
    now = now or datetime.now()
    lines: list[str] = []
    slot: datetime | None = None
    try:
        slot = pick_free_slot(fetch_today_events(bridge, now), now)
    except BridgeError as exc:
        lines.append(f"Keine Kalenderprüfung: {exc}")

    if slot:
        end = slot + timedelta(minutes=SLOT_MIN)
        lines.append(f"Freies Fenster heute: {slot:%H:%M}–{end:%H:%M}")
    else:
        lines.append("Heute war kein freies 15-Minuten-Fenster.")

    due = slot or _fallback_due(now)
    try:
        lines.append(
            bridge.call_tool(
                "reminders_add",
                {"title": REMINDER_TITLE_FMT.format(topic=topic), "due": due.isoformat(timespec="minutes")},
            )
        )
    except BridgeError as exc:
        lines.append(f"Erinnerung fehlgeschlagen: {exc}")

    if slot:
        try:
            lines.append(
                bridge.call_tool(
                    "calendar_add",
                    {
                        "title": EVENT_TITLE,
                        "start": slot.isoformat(timespec="minutes"),
                        "duration_min": SLOT_MIN,
                    },
                )
            )
        except BridgeError as exc:
            lines.append(f"Kalender fehlgeschlagen: {exc}")
    return " · ".join(lines)


def _fallback_due(now: datetime) -> datetime:
    """No free slot (or calendar unreadable): nudge at 20:00, else tomorrow 09:00."""
    evening = datetime.combine(now.date(), time(20, 0))
    if evening > now:
        return evening
    return datetime.combine(now.date() + timedelta(days=1), time(9, 0))
