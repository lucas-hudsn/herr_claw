"""Writes tutor artifacts to the Obsidian vault — only via the bridge's
scoped vault.append (append-only, allowlisted, audited).

Fixed filenames (AGENTS.md): Deutsch/progress.md, Deutsch/fehler.md,
Daily notes/YYYY-MM-DD-deutsch.md. vault stays None when HERR_VAULT is
unset (state/ still persists — chat keeps working).
"""

from __future__ import annotations

from datetime import datetime

from herrclaw_bridge.vault import Vault

from .state import SrsState

PROGRESS_FILE = "Deutsch/progress.md"
FEHLER_FILE = "Deutsch/fehler.md"
DAILY_NOTE_FMT = "Daily notes/%Y-%m-%d-deutsch.md"

PROGRESS_HEADER = "# Deutsch — Fortschritt\n\n"
FEHLER_HEADER = "# Deutsch — Fehlerlog\n\n"


def safe_append(vault: Vault | None, rel_path: str, text: str, header: str | None = None) -> bool:
    """Append; write `header` first when the file doesn't exist yet. On IO
    problems warn once — state/ keeps persisting either way. VaultDenied is
    an OSError too. Returns False when the write did not happen."""
    if vault is None:
        return False
    if header is not None:
        try:
            vault.read(rel_path)
        except FileNotFoundError:
            vault.append(rel_path, header)
        except OSError:
            pass  # file exists but unreadable — append will surface it
    try:
        vault.append(rel_path, text)
        return True
    except OSError as exc:
        print(f"⚠︎ Obsidian-Schreibzugriff fehlgeschlagen ({exc}) — Notizen deaktiviert.")
        return False


class Tracker:
    def __init__(self, vault: Vault | None, srs: SrsState) -> None:
        self.vault = vault
        self.srs = srs
        self.session_corrections = 0
        self.session_turns = 0

    def note_exchange(self) -> None:
        self.session_turns += 1

    def log_correction(self, example: str, fix: str, pattern: str = "") -> None:
        is_new = self.srs.add_mistake(example=example, fix=fix, pattern=pattern)
        self.session_corrections += 1
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
        """Called once when the chat loop exits: updates the streak, then
        appends the session summary to the daily note and progress.md."""
        if self.session_turns == 0:
            return
        self.srs.touch_day()
        self.srs.totals.sessions += 1
        if not self.vault:
            return
        now = datetime.now()
        corrections = (
            f"{self.session_corrections} Korrektur"
            if self.session_corrections == 1
            else f"{self.session_corrections} Korrekturen"
        )
        daily_rel = now.strftime(DAILY_NOTE_FMT)
        daily = (
            f"\n## Chat mit Herr Claw — {now.strftime('%H:%M')}\n"
            f"- {self.session_turns} Nachrichten, {corrections}\n"
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
                f"{self.session_turns} Nachrichten, {corrections} · "
                f"Streak: {self.srs.streak.current} Tag(e) · "
                f"Gesamt: {self.srs.totals.messages} Nachrichten, "
                f"{self.srs.totals.corrections} Korrekturen"
            ),
            header=PROGRESS_HEADER,
        )

    def _safe_append(self, rel_path: str, text: str, header: str | None = None) -> None:
        """Delegate to safe_append; on failure disable vault writes for the
        session (state/ keeps persisting)."""
        if not safe_append(self.vault, rel_path, text, header=header):
            self.vault = None
