"""reminders.py — Deutsch-list scoping, AppleScript generation, audit."""

import json

import pytest

from herrclaw_bridge import reminders
from herrclaw_bridge.errors import BridgeError


@pytest.fixture
def scripts(monkeypatch):
    """Record every AppleScript; fake plausible results by script shape."""
    ran = []

    def fake_run(script):
        ran.append(script)
        if "whose completed is false and name contains" in script:
            return "2"  # complete_reminder count
        if "whose completed is false" in script:
            return reminders.LIST_MISSING  # list_reminders without the list
        return ""  # add_reminder confirmation unused

    monkeypatch.setattr(reminders, "run_applescript", fake_run)
    return ran


def test_add_escapes_quotes_in_title(scripts, tmp_path):
    out = reminders.add_reminder('Sag "Hallo" \\ Tschüss', log_path=tmp_path / "a.log")
    assert '\\"Hallo\\"' in scripts[0]  # quotes escaped for AppleScript
    assert "\\\\ Tschüss" in scripts[0]  # backslash escaped before quote-escaping
    assert "Tschüss" in out


def test_add_with_due_builds_date_block(scripts, tmp_path):
    reminders.add_reminder("Deutsch: 5-Min Chat — Thema Bäckerei", due="2026-10-02 14:30", log_path=tmp_path / "a.log")
    script = scripts[0]
    assert "set year of dueDate to 2026" in script
    assert "set month of dueDate to 10" in script
    assert "set day of dueDate to 2" in script
    assert "due date:dueDate" in script
    assert "make new list" in script  # list auto-created if missing


def test_add_without_due_has_no_date_block(scripts, tmp_path):
    reminders.add_reminder("Nur so", log_path=tmp_path / "a.log")
    assert "dueDate" not in scripts[0]


def test_add_empty_title_denied_and_audited(scripts, tmp_path):
    with pytest.raises(BridgeError):
        reminders.add_reminder("   ", log_path=tmp_path / "a.log")
    assert scripts == []  # never reached osascript
    entry = json.loads((tmp_path / "a.log").read_text().strip())
    assert entry["allowed"] is False and entry["action"] == "reminders.add"


def test_add_audits_allow(scripts, tmp_path):
    reminders.add_reminder("Brot kaufen", log_path=tmp_path / "a.log")
    entry = json.loads((tmp_path / "a.log").read_text().strip())
    assert entry == {
        **entry,
        "action": "reminders.add",
        "target": "Deutsch: Brot kaufen",
        "allowed": True,
    }


def test_list_passes_through_items(scripts, tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "run_applescript", lambda s: "• Bäckerei üben\n")
    out = reminders.list_reminders(log_path=tmp_path / "a.log")
    assert "Bäckerei üben" in out


def test_list_empty(scripts, tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "run_applescript", lambda s: "")
    assert reminders.list_reminders(log_path=tmp_path / "a.log") == "Keine offenen Erinnerungen in „Deutsch“."


def test_list_missing_list_is_honest(scripts, tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "run_applescript", lambda s: reminders.LIST_MISSING)
    out = reminders.list_reminders(log_path=tmp_path / "a.log")
    assert "gibt es noch nicht" in out


def test_complete_reports_count(scripts, tmp_path):
    out = reminders.complete_reminder("5-Min Chat", log_path=tmp_path / "a.log")
    assert "2" in out and "abgehakt" in out
    assert 'whose completed is false and name contains' in scripts[0]


def test_complete_zero_matches(scripts, tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "run_applescript", lambda s: "0")
    out = reminders.complete_reminder("Nirvana", log_path=tmp_path / "a.log")
    assert "Keine offene Erinnerung" in out
