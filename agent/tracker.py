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

_PROGRESS_HEADER = "# Deutsch — Fortschritt\n\n"
_FEHLER_HEADER = "# Deutsch — Fehlerlog\n\n"


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
        self._safe_append(
            FEHLER_FILE,
            f"- {stamp} — Deine Version: „{example}“ → Richtig: „{fix}“",
            header=_FEHLER_HEADER,
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
        self._safe_append(daily_rel, daily)
        self._safe_append(
            PROGRESS_FILE,
            (
                f"- {now.strftime('%Y-%m-%d')} — Sitzung {now.strftime('%H:%M')}: "
                f"{self.session_turns} Nachrichten, {corrections} · "
                f"Streak: {self.srs.streak.current} Tag(e) · "
                f"Gesamt: {self.srs.totals.messages} Nachrichten, "
                f"{self.srs.totals.corrections} Korrekturen"
            ),
            header=_PROGRESS_HEADER,
        )

    def _safe_append(self, rel_path: str, text: str, header: str | None = None) -> None:
        """Append; on IO problems warn once and disable vault writes for the
        session (state/ keeps persisting). VaultDenied is an OSError too."""
        if self.vault is None:
            return
        if header is not None:
            try:
                self.vault.read(rel_path)
            except FileNotFoundError:
                self.vault.append(rel_path, header)
            except OSError:
                pass  # file exists but unreadable — append will surface it
        try:
            self.vault.append(rel_path, text)
        except OSError as exc:
            print(f"⚠︎ Obsidian-Schreibzugriff fehlgeschlagen ({exc}) — Notizen deaktiviert.")
            self.vault = None
