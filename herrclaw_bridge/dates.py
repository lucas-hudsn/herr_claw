"""Local-time parsing shared by the reminders and calendar components.

The bridge speaks ISO strings over MCP ("2026-10-02", "2026-10-02 14:30")
and local datetimes to Apple apps. Everything is naive-local: reminders and
Calendar events are created in the user's wall-clock time, never UTC.
"""

from __future__ import annotations

from datetime import datetime, time

from .errors import BridgeError

DEFAULT_TIME = time(9, 0)  # date-only input falls back to 09:00 local


def parse_local(value: str) -> datetime:
    """Parse an ISO date or datetime string as LOCAL time. Date-only inputs
    get DEFAULT_TIME. Anything unparseable is a BridgeError, not a ValueError
    — callers turn it into an audited tool error."""
    value = (value or "").strip()
    if not value:
        raise BridgeError("Zeitangabe fehlt (erwartet: YYYY-MM-DD [HH:MM]).")
    try:
        parsed = datetime.fromisoformat(value.replace("T", " "))
    except ValueError:
        raise BridgeError(f"Zeitangabe unverständlich: '{value}' (erwartet: YYYY-MM-DD [HH:MM]).")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    if "T" not in value and " " not in value and ":" not in value:
        parsed = datetime.combine(parsed.date(), DEFAULT_TIME)
    return parsed
