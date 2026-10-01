"""Apple Reminders via osascript — scoped to the ONE 'Deutsch' list.

SPEC §4.5: the bridge creates/uses the list 'Deutsch'; no other list is
read or written, so unlike the vault there is no caller-visible allowlist
parameter to misuse — the scope IS this module. Every call (including
failures) lands in the audit log; the daily loop's mark-complete at 20:00
(SPEC §4.1) shows up here as reminders.complete.
"""

from __future__ import annotations

from .applescript import qa, run_applescript
from .audit import DEFAULT_AUDIT_LOG, audit
from .dates import parse_local
from .errors import BridgeError

DEUTSCH_LIST = "Deutsch"

LIST_MISSING = "LIST_MISSING"


def _audited(action: str, target: str, log_path):
    def record(allowed: bool, reason: str | None = None) -> None:
        audit(action, target, allowed=allowed, reason=reason, log_path=log_path)

    return record


def _ensure_list_script() -> str:
    return (
        'tell application "Reminders"\n'
        f'    if not (exists list "{qa(DEUTSCH_LIST)}") then\n'
        f'        make new list with properties {{name:"{qa(DEUTSCH_LIST)}"}}\n'
        "    end if\n"
        "end tell"
    )


def list_reminders(log_path=DEFAULT_AUDIT_LOG) -> str:
    """Open reminders in the Deutsch list, one per line. Missing list is not
    an error — it means nothing was scheduled yet."""
    record = _audited("reminders.list", DEUTSCH_LIST, log_path)
    record(allowed=True)
    script = (
        "tell application \"Reminders\"\n"
        f'    if not (exists list "{qa(DEUTSCH_LIST)}") then return "{LIST_MISSING}"\n'
        "    set out to \"\"\n"
        f"    repeat with r in (reminders of list \"{qa(DEUTSCH_LIST)}\" whose completed is false)\n"
        '        set out to out & "• " & (name of r)\n'
        "        try\n"
        "            set d to due date of r\n"
        '            if d is not missing value then set out to out & " — fällig " & (d as string)\n'
        "        end try\n"
        "        set out to out & linefeed\n"
        "    end repeat\n"
        "    return out\n"
        "end tell"
    )
    out = run_applescript(script)
    if LIST_MISSING in out:
        return f"Die Liste „{DEUTSCH_LIST}“ gibt es noch nicht — keine offenen Erinnerungen."
    out = out.strip()
    return out or "Keine offenen Erinnerungen in „Deutsch“."


def add_reminder(title: str, due: str = "", log_path=DEFAULT_AUDIT_LOG) -> str:
    """Create a reminder in the Deutsch list (list is created on first use).
    `due` is optional ISO date or datetime (dates.py). Returns a short
    German confirmation for the tutor to relay."""
    title = (title or "").strip()
    record = _audited("reminders.add", f"{DEUTSCH_LIST}: {title}", log_path)
    if not title:
        record(allowed=False, reason="empty title")
        raise BridgeError("Titel der Erinnerung fehlt.")
    record(allowed=True)

    props = f'name:"{qa(title)}"'
    if due.strip():
        when = parse_local(due)
        props += ", due date:dueDate"
        date_block = _apple_date_block("dueDate", when)
    else:
        date_block = ""

    script = (
        date_block
        + "\n"
        + "tell application \"Reminders\"\n"
        f'    if not (exists list "{qa(DEUTSCH_LIST)}") then\n'
        f'        make new list with properties {{name:"{qa(DEUTSCH_LIST)}"}}\n'
        "    end if\n"
        f"    tell list \"{qa(DEUTSCH_LIST)}\"\n"
        f"        make new reminder with properties {{{props}}}\n"
        "    end tell\n"
        "end tell"
    )
    run_applescript(script)
    conf = f"Erinnerung in „{DEUTSCH_LIST}“ angelegt: {title}"
    if due.strip():
        conf += f" (fällig {parse_local(due).strftime('%d.%m. %H:%M')})"
    return conf


def complete_reminder(title: str, log_path=DEFAULT_AUDIT_LOG) -> str:
    """Mark every OPEN reminder in the Deutsch list whose name contains
    `title` as completed. Returns how many were matched."""
    title = (title or "").strip()
    record = _audited("reminders.complete", f"{DEUTSCH_LIST}: {title}", log_path)
    if not title:
        record(allowed=False, reason="empty title")
        raise BridgeError("Titel der Erinnerung fehlt.")
    record(allowed=True)
    script = (
        "tell application \"Reminders\"\n"
        f'    if not (exists list "{qa(DEUTSCH_LIST)}") then return "0"\n'
        f"    set matches to (reminders of list \"{qa(DEUTSCH_LIST)}\" whose completed is false and name contains \"{qa(title)}\")\n"
        "    repeat with r in matches\n"
        "        set completed of r to true\n"
        "    end repeat\n"
        "    return (count of matches) as string\n"
        "end tell"
    )
    count = int(run_applescript(script) or "0")
    if count == 0:
        return f"Keine offene Erinnerung zu „{title}“ in „{DEUTSCH_LIST}“ gefunden."
    if count == 1:
        return f"Erinnerung „{title}“ abgehakt. ✅"
    return f"{count} Erinnerungen zu „{title}“ abgehakt. ✅"


def _apple_date_block(var: str, when) -> str:
    """AppleScript snippet building a local date in `var` from a datetime.
    Component-wise assignment avoids locale-dependent `date "..."` parsing;
    day last so values like Feb 30 can't overflow."""
    return (
        f"set {var} to current date\n"
        f"set year of {var} to {when.year}\n"
        f"set month of {var} to {when.month}\n"
        f"set day of {var} to {when.day}\n"
        f"set time of {var} to {when.hour * 3600 + when.minute * 60 + when.second}"
    )
