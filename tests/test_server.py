"""End-to-end bridge test: real MCP Streamable-HTTP server (uvicorn thread)
+ real BridgeClient, with the Apple backends faked at module level.

This is the P2 wiring proof: tool registration, error-result → BridgeError
mapping (policy denials included), and vault enforcement across the wire.
Never touches the real vault, Reminders, or Calendar.
"""

import json
import socket
import threading
import time

import pytest
import uvicorn

from agent.bridge import BridgeClient, BridgeError
from herrclaw_bridge import calendar_apple, reminders, server as bridge_server
from herrclaw_bridge.vault import Vault


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running_bridge(monkeypatch, tmp_path):
    """Bridge server on an ephemeral port; Apple calls faked; real tmp vault."""
    vault = Vault(tmp_path / "vault", audit_log=tmp_path / "audit.log")

    monkeypatch.setattr(reminders, "list_reminders", lambda log_path=None: "• Deutsch: 5-Min Chat — Thema Bäckerei")
    monkeypatch.setattr(
        reminders, "add_reminder", lambda title, due="", log_path=None: f"Erinnerung angelegt: {title}"
    )
    monkeypatch.setattr(
        reminders, "complete_reminder", lambda title, log_path=None: f"Abgehakt: {title}"
    )
    monkeypatch.setattr(
        calendar_apple,
        "freebusy",
        lambda start, end, log_path=None: json.dumps({"start": start, "end": end, "events": []}),
    )
    monkeypatch.setattr(
        calendar_apple,
        "add_event",
        lambda title, start, duration_min=15, log_path=None: f"Termin angelegt: {title} @ {start}",
    )

    app = bridge_server.build_app(vault)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    if not srv.started:
        raise RuntimeError("uvicorn did not start")

    yield BridgeClient(f"http://127.0.0.1:{port}/mcp", timeout_s=10.0), vault, tmp_path

    srv.should_exit = True
    thread.join(timeout=5)


def test_all_tools_across_the_wire(running_bridge):
    client, _vault, _tmp = running_bridge
    assert "Bäckerei" in client.call_tool("reminders_list")
    assert client.call_tool("reminders_add", {"title": "X", "due": "2026-10-02 14:30"}) == "Erinnerung angelegt: X"
    assert "X" in client.call_tool("reminders_complete", {"title": "X"})
    assert json.loads(client.call_tool("calendar_freebusy", {"start": "2026-10-02", "end": "2026-10-03"}))["events"] == []
    assert "Termin angelegt" in client.call_tool("calendar_add", {"title": "T", "start": "2026-10-02 09:00"})


def test_run_job_trigger_path_across_the_wire(running_bridge, monkeypatch):
    """P5: an OpenClaw-cron fire (in the sandbox) reaches run_job over MCP;
    the tool forwards the name and returns the host-side result text. The
    engine itself is unit-tested in test_jobs.py — here we prove the wiring
    and that the server hands its own (allowlisted) vault to the engine."""
    import agent.jobs as jobs_mod

    seen: dict = {}

    def fake_run_job_once(name, vault=None, **_kwargs):
        seen["name"] = name
        seen["vault_root"] = vault.root
        return f"Job „{name}“ erledigt."

    monkeypatch.setattr(jobs_mod, "run_job_once", fake_run_job_once)
    client, vault, _tmp = running_bridge
    assert "erledigt" in client.call_tool("run_job", {"name": "recap"})
    assert seen == {"name": "recap", "vault_root": vault.root}


def test_run_job_denial_comes_back_as_german_error(running_bridge, monkeypatch):
    import agent.jobs as jobs_mod
    from herrclaw_bridge.errors import BridgeError as LocalBridgeError

    def denying(_name, **_kwargs):
        raise LocalBridgeError("Unbekannter Job „x“ — erlaubt sind: nudge, quiz, recap.")

    monkeypatch.setattr(jobs_mod, "run_job_once", denying)
    client, _vault, _tmp = running_bridge
    with pytest.raises(BridgeError, match="Unbekannter Job"):
        client.call_tool("run_job", {"name": "x"})


def test_vault_roundtrip_across_the_wire(running_bridge):
    client, vault, _tmp = running_bridge
    assert "Angehängt" in client.call_tool("vault_append", {"rel_path": "Deutsch/fehler.md", "text": "zeile"})
    assert "zeile" in client.call_tool("vault_read", {"rel_path": "Deutsch/fehler.md"})
    assert (vault.root / "Deutsch" / "fehler.md").exists()


def test_vault_deny_across_the_wire(running_bridge):
    """The demo's deny moment, over real MCP: out-of-scope write comes back
    as an error result carrying the German denial — and writes nothing."""
    client, vault, tmp_path = running_bridge
    with pytest.raises(BridgeError, match="verweigert"):
        client.call_tool("vault_append", {"rel_path": "Books/todo.md", "text": "nope"})
    assert not (vault.root / "Books").exists()
    entries = [json.loads(line) for line in (tmp_path / "audit.log").read_text().strip().splitlines()]
    denied = [e for e in entries if e["action"] == "vault.append" and not e["allowed"]]
    assert denied and denied[0]["target"] == "Books/todo.md"


def test_bridge_without_vault_disables_vault_tools(monkeypatch, tmp_path):
    app = bridge_server.build_app(None)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if srv.started:
                break
            time.sleep(0.05)
        client = BridgeClient(f"http://127.0.0.1:{port}/mcp", timeout_s=10.0)
        with pytest.raises(BridgeError, match="HERR_VAULT"):
            client.call_tool("vault_read", {"rel_path": "Deutsch/progress.md"})
    finally:
        srv.should_exit = True
        thread.join(timeout=5)


def test_transport_security_blocks_foreign_host():
    """The bridge reached under the OpenShell host alias (P5) must 421 foreign
    Host headers (DNS-rebinding protection) while the alias itself passes."""
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.testclient import TestClient

    app = bridge_server.build_app(
        None,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:8765", "host.openshell.internal:8765"],
            allowed_origins=["http://host.openshell.internal:8765"],
        ),
    )
    with TestClient(app) as client:
        evil = client.post(
            "/mcp",
            headers={"Host": "evil.example:8765", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        )
        assert evil.status_code == 421
        ok = client.post(
        "/mcp",
        headers={
            "Host": "host.openshell.internal:8765",
            "Origin": "http://host.openshell.internal:8765",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        )
        assert ok.status_code != 421
