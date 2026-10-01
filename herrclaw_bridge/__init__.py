"""herrclaw_bridge — host-side MCP bridge (Apple apps + vault).

Enforcement lives in the component modules, never in the transport:
- vault.py: path-scoped append-only Obsidian access (P1)
- reminders.py: Apple Reminders via osascript, scoped to the 'Deutsch' list (P2)
- calendar_apple.py: EventKit freebusy/add + AppleScript fallback (P2)
- audit.py: every call, allowed or denied, lands in ~/.herr-claw/audit.log
- server.py: exposes all of it as MCP tools over Streamable-HTTP on
  127.0.0.1:8765 (`herr-claw bridge`); agent code goes through agent/bridge.py,
  never around it.
"""
