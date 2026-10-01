"""Vault allowlist + append-only + audit tests — the P1 deny proof."""

import json

import pytest

from herrclaw_bridge.vault import Vault, VaultDenied


class TestAppendAllow:
    @pytest.mark.parametrize("rel", ["Deutsch/fehler.md", "Daily notes/2026-10-01-deutsch.md", "Weeks/2026-W40-deutsch-review.md"])
    def test_allows_scoped_dirs(self, vault, config, rel):
        out = vault.append(rel, "hallo")
        assert out.exists()
        assert out.is_relative_to(config.vault_root.expanduser().resolve())
        assert out.read_text(encoding="utf-8") == "hallo\n"  # trailing newline guaranteed

    def test_append_never_truncates(self, vault):
        vault.append("Deutsch/fehler.md", "erste Zeile")
        vault.append("Deutsch/fehler.md", "zweite Zeile")
        content = vault.read("Deutsch/fehler.md")
        assert "erste Zeile" in content and "zweite Zeile" in content

    def test_creates_parent_dirs_inside_scope(self, vault):
        vault.append("Deutsch/sub/ordnung/fehler.md", "x")
        assert vault.read("Deutsch/sub/ordnung/fehler.md") == "x\n"

    def test_read_within_scope(self, vault):
        vault.append("Deutsch/vocab.md", "# Vokabeln")
        assert vault.read("Deutsch/vocab.md") == "# Vokabeln\n"

    def test_read_missing_in_scope_raises_filenotfound(self, vault):
        with pytest.raises(FileNotFoundError):
            vault.read("Deutsch/nope.md")


class TestAppendDeny:
    @pytest.mark.parametrize("rel", [
        "Books/todo.md",
        "Courses/x.md",
        "Goals/y.md",
        "Projects/z.md",
        ".obsidian/config.json",
        "Random/not-allowed.md",
        "../outside.md",
        "Deutsch/../../escape.md",
        "Daily notes/../Weeks-impersonation.md",
        "/etc/passwd",
        "C:/Windows/x.md",
        "~/Obsidian/vault/Deutsch/x.md",
        "",
    ])
    def test_denied_outside_allowlist(self, vault, rel):
        with pytest.raises(VaultDenied):
            vault.append(rel, "nope")

    def test_denied_write_writes_nothing(self, vault):
        with pytest.raises(VaultDenied):
            vault.append("Books/todo.md", "nope")
        assert not (vault.root / "Books").exists()

    def test_read_denied_outside_allowlist(self, vault):
        with pytest.raises(VaultDenied):
            vault.read(".obsidian/app.json")


class TestAudit:
    def test_allow_and_deny_both_audited(self, vault, tmp_path):
        vault.append("Deutsch/fehler.md", "ok")
        with pytest.raises(VaultDenied):
            vault.append("Books/todo.md", "nope")
        lines = (tmp_path / "audit.log").read_text(encoding="utf-8").strip().splitlines()
        entries = [json.loads(line) for line in lines]
        assert [e["allowed"] for e in entries] == [True, False]
        assert entries[0]["action"] == "vault.append"
        assert entries[1]["action"] == "vault.append"
        assert entries[1]["target"] == "Books/todo.md"
        assert "out of scope" in entries[1]["reason"]
        assert all("ts" in e for e in entries)


def test_no_mutation_api_on_vault():
    """Append-only by construction: there is no edit/delete/write API to misuse."""
    for banned in ("delete", "remove", "write", "edit", "truncate", "unlink", "rmtree"):
        assert not hasattr(Vault, banned)
