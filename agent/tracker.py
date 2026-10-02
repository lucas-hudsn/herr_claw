"""Writes tutor artifacts to the Obsidian vault — only via the bridge's
scoped vault.append (append-only, allowlisted, audited).

Fixed filenames (AGENTS.md): Deutsch/progress.md, Deutsch/fehler.md,
Daily notes/YYYY-MM-DD-deutsch.md. vault stays None when HERR_VAULT is
unset (state/ still persists — chat keeps working).
"""

from __future__ import annotations

from datetime import datetime

from herrclaw_bridge.errors import BridgeError
from herrclaw_bridge.vault import Vault

from .state import SessionState, SrsState

PROGRESS_FILE = "Deutsch/progress.md"
FEHLER_FILE = "Deutsch/fehler.md"
DAILY_NOTE_FMT = "Daily notes/%Y-%m-%d-deutsch.md"

PROGRESS_HEADER = "# Deutsch — Fortschritt\n\n"
FEHLER_HEADER = "# Deutsch — Fehlerlog\n\n"


def safe_append(vault, rel_path: str, text: str, header: str | None = None) -> bool:
    """Append; write `header` first when the file doesn't exist yet. On IO
    problems warn once — state/ keeps persisting either way. VaultDenied is
    an OSError; BridgeError covers the in-sandbox path where vault reads and
    writes cross the bridge instead of the filesystem (a missing file read
    arrives as a bridge tool error there). Returns False when the write did
    not happen."""
    if vault is None:
        return False
    try:
        if header is not None:
            try:
                vault.read(rel_path)
            except (FileNotFoundError, BridgeError):
                vault.append(rel_path, header)
            except OSError:
                pass  # file exists but unreadable — append will surface it
        vault.append(rel_path, text)
        return True
    except (OSError, BridgeError) as exc:
        print(f"⚠︎ Obsidian-Schreibzugriff fehlgeschlagen ({exc}) — Notizen deaktiviert.")
        return False


class Tracker:
    """Session bookkeeping. With `session` set (the one-shot turn path), the
    per-session counts live in session.json — they survive across processes,
    so a session opened in the TUI can be closed hours later by the Telegram
    poller's gap-close and still report honest totals."""

    def __init__(self, vault, srs: SrsState, session: SessionState | None = None) -> None:
        self.vault = vault
        self.srs = srs
        self.session_state = session
        self.session_corrections = 0
        self.session_turns = 0

    def note_exchange(self) -> None:
        self.session_turns += 1
        if self.session_state is not None:
            self.session_state.session_turns += 1

    def log_correction(self, example: str, fix: str, pattern: str = "") -> None:
        is_new = self.srs.add_mistake(example=example, fix=fix, pattern=pattern)
        self.session_corrections += 1
        if self.session_state is not None:
            self.session_state.session_corrections += 1
        if not (self.vault and is_new):
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        safe_append(
            self.vault,
            FEHLER_FILE,
            f"- {stamp} — Deine Version: „{example}“ → Richtig: „{fix}“",
            header=FEHLER_HEADER,
        )

    def end_session(self, topic: str = "") -> None:
        """Close the open session: updates the streak, then appends the
        session summary to the daily note and progress.md. Counts come from
        session.json when a session is attached (cross-process truth)."""
        if self.session_state is not None:
            self.session_turns = self.session_state.session_turns
            self.session_corrections = self.session_state.session_corrections
        if self.session_turns == 0:
            return
        turns, corrections_count = self.session_turns, self.session_corrections
        self.srs.touch_day()
        self.srs.totals.sessions += 1
        self._reset_counts()
        if not self.vault:
            return
        now = datetime.now()
        corrections = (
            f"{corrections_count} Korrektur"
            if corrections_count == 1
            else f"{corrections_count} Korrekturen"
        )
        daily_rel = now.strftime(DAILY_NOTE_FMT)
        daily = (
            f"\n## Chat mit Herr Claw — {now.strftime('%H:%M')}\n"
            f"- {turns} Nachrichten, {corrections}\n"
            f"- Streak: {self.srs.streak.current} Tag(e)\n"
        )
        if topic:
            daily += f"- Thema: {topic}\n"
        safe_append(self.vault, daily_rel, daily)
        safe_append(
            self.vault,
            PROGRESS_FILE,
            (
                f"- {now.strftime('%Y-%m-%d')} — Sitzung {now.strftime('%H:%M')}: "
                f"{turns} Nachrichten, {corrections} · "
                f"Streak: {self.srs.streak.current} Tag(e) · "
                f"Gesamt: {self.srs.totals.messages} Nachrichten, "
                f"{self.srs.totals.corrections} Korrekturen"
            ),
            header=PROGRESS_HEADER,
        )

    def _reset_counts(self) -> None:
        self.session_turns = 0
        self.session_corrections = 0
        if self.session_state is not None:
            self.session_state.session_turns = 0
            self.session_state.session_corrections = 0

    def _safe_append(self, rel_path: str, text: str, header: str | None = None) -> None:
        """Delegate to safe_append; on failure disable vault writes for the
        session (state/ keeps persisting)."""
        if not safe_append(self.vault, rel_path, text, header=header):
            self.vault = None
