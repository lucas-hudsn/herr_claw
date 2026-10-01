"""Path-scoped Obsidian vault access. The allowlist IS the governance.

Write access is append-only (`vault.append`, no edit/delete API by design)
and restricted to Deutsch/, Daily notes/ and Weeks/ inside HERR_VAULT.
Everything else — including Books/, Courses/, Goals/, Projects/, .obsidian/,
the second vault ~/Obsidian/vault, and anything outside the vault root — is
denied. Every call, allowed or denied, is written to the audit log.

In P1 the tutor uses this module in-process; from P2 the same module is
exposed over the MCP bridge on localhost:8765 with identical enforcement.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .audit import DEFAULT_AUDIT_LOG, audit

ALLOWED_TOP_LEVEL = ("Deutsch", "Daily notes", "Weeks")
FORBIDDEN_TOP_LEVEL = ("Books", "Courses", "Goals", "Projects", ".obsidian")


class VaultDenied(PermissionError):
    """A vault path outside the allowlist was requested."""


class Vault:
    def __init__(self, root: str | Path, audit_log: Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.audit_log = Path(audit_log) if audit_log else DEFAULT_AUDIT_LOG

    def _resolve(self, rel_path: str, action: str) -> Path:
        rel = rel_path.strip().lstrip("/")
        target = None
        reason: str | None = None
        if not rel:
            reason = "empty path"
        elif "\\" in rel:
            reason = "path separator escape"
        elif ".." in PurePosixPath(rel).parts:
            reason = "path traversal"
        else:
            pure = PurePosixPath(rel)
            top = pure.parts[0] if pure.parts else ""
            if top in FORBIDDEN_TOP_LEVEL:
                reason = f"'{top}/' is out of scope"
            elif top not in ALLOWED_TOP_LEVEL:
                reason = (
                    f"top-level '{top}' not in allowlist "
                    f"({', '.join(ALLOWED_TOP_LEVEL)})"
                )
            else:
                target = self.root.joinpath(*pure.parts)
        if target is None:
            assert reason
            audit(action, rel_path, allowed=False, reason=reason, log_path=self.audit_log)
            raise VaultDenied(f"vault: '{rel_path}' verweigert — {reason}")
        audit(action, rel_path, allowed=True, log_path=self.audit_log)
        return target

    def append(self, rel_path: str, text: str) -> Path:
        """Append text to an allowlisted file, creating it (and its parent
        dirs inside the allowlist) if needed. Never truncates."""
        target = self._resolve(rel_path, "vault.append")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not text.endswith("\n"):
            text += "\n"
        with target.open("a", encoding="utf-8") as fh:
            fh.write(text)
        return target

    def read(self, rel_path: str) -> str:
        """Read an allowlisted file. Missing files raise FileNotFoundError
        like any normal read; policy violations raise VaultDenied."""
        target = self._resolve(rel_path, "vault.read")
        return target.read_text(encoding="utf-8")
