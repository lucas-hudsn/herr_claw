"""Audit trail for every governed bridge call — allowed AND denied.

Every vault (and later Reminders/Calendar) call lands as one JSON line in
~/.herr-claw/audit.log. The governance "deny moment" in the demo is read
from this file, so it must never be optional.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

DEFAULT_AUDIT_LOG = Path(os.path.expanduser("~/.herr-claw/audit.log"))


def audit(
    action: str,
    target: str,
    allowed: bool,
    reason: str | None = None,
    log_path: Path | None = None,
) -> None:
    """Append one JSONL audit entry. Never raises on audit failure of the
    caller's path — a broken audit log must not silently disable policy
    checks, but IO problems here surface to the operator."""
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "action": action,
        "target": target,
        "allowed": allowed,
    }
    if reason:
        entry["reason"] = reason
    path = Path(log_path) if log_path else DEFAULT_AUDIT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
