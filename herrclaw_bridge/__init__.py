"""herrclaw_bridge — host-side MCP bridge components (Apple apps + vault).

P1 ships the vault component (path-scoped append-only FS access + audit);
P2 adds server.py (Streamable-HTTP MCP on localhost:8765), reminders.py and
calendar_apple.py behind the same audit trail.
"""
