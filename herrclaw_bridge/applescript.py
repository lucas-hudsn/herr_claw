"""The single osascript gateway of the whole project.

Agent code never calls osascript (AGENTS.md invariant) — only these bridge
components do, and every call is audited by the component layer above this
runner. All AppleScript string literals must go through `qa()` (quote
escaping) so learner-generated titles can't inject script code.
"""

from __future__ import annotations

import subprocess

from .errors import BridgeError

OSASCRIPT_TIMEOUT_S = 30


def qa(text: str) -> str:
    """Escape a Python string as an AppleScript string literal body."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def run_applescript(script: str) -> str:
    """Run an AppleScript via osascript and return its stdout. Non-zero exit
    (app missing, TCC denied, syntax) becomes BridgeError with the stderr
    hint — osascript's stderr is where the useful diagnosis lives."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=OSASCRIPT_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise BridgeError("osascript nicht gefunden — macOS vorausgesetzt.") from exc
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"osascript nach {OSASCRIPT_TIMEOUT_S}s abgebrochen.") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise BridgeError(f"AppleScript fehlgeschlagen: {detail[-1] if detail else 'unbekannter Fehler'}")
    return (proc.stdout or "").strip()
