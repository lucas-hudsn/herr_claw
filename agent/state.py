"""Tutor memory — the ONE state path (AGENTS.md hard invariant).

state/srs.json    learning state: vocab (SRS), mistakes, streak, totals
state/session.json session state: last session, current topic, pause

Quiz/SRS rules (AGENTS.md): SM-2-lite, L1→1d L2→3d L3→7d L4→14d L5→30d;
a miss drops one level and resets the due date. Quiz content stays inside
seed + state vocab (enforced where quizzes are generated, P4).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

INTERVALS_DAYS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
MAX_LEVEL = 5


@dataclass
class VocabEntry:
    en: str
    level: int = 1
    next_due: date | None = None
    misses: int = 0
    article: str = ""  # der/die/das — nouns always carry article + plural
    plural: str = ""


@dataclass
class Mistake:
    example: str  # the learner's version
    fix: str  # the corrected version
    pattern: str = ""
    count: int = 1
    first_seen: str = ""
    last_seen: str = ""


@dataclass
class Streak:
    current: int = 0
    longest: int = 0
    last_day: date | None = None


@dataclass
class Totals:
    sessions: int = 0
    messages: int = 0
    corrections: int = 0


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class SrsState:
    """Load/save srs.json with atomic writes; corrupt files are backed up
    beside the original and reset (never silently dropped)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.vocab: dict[str, VocabEntry] = {}
        self.mistakes: list[Mistake] = []
        self.streak = Streak()
        self.totals = Totals()
        self._load()

    # -- persistence ----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.vocab = {
                de: VocabEntry(
                    en=item["en"],
                    level=int(item.get("level", 1)),
                    next_due=_parse_date(item.get("next_due")),
                    misses=int(item.get("misses", 0)),
                    article=item.get("article", ""),
                    plural=item.get("plural", ""),
                )
                for de, item in raw.get("vocab", {}).items()
            }
            self.mistakes = [
                Mistake(
                    example=m["example"],
                    fix=m["fix"],
                    pattern=m.get("pattern", ""),
                    count=int(m.get("count", 1)),
                    first_seen=m.get("first_seen", ""),
                    last_seen=m.get("last_seen", ""),
                )
                for m in raw.get("mistakes", [])
            ]
            streak = raw.get("streak", {})
            self.streak = Streak(
                current=int(streak.get("current", 0)),
                longest=int(streak.get("longest", 0)),
                last_day=_parse_date(streak.get("last_day")),
            )
            totals = raw.get("totals", {})
            self.totals = Totals(
                sessions=int(totals.get("sessions", 0)),
                messages=int(totals.get("messages", 0)),
                corrections=int(totals.get("corrections", 0)),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            backup = self.path.with_suffix(f".json.corrupt-{int(time.time())}")
            self.path.rename(backup)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "vocab": {
                de: {
                    **asdict(entry),
                    "next_due": entry.next_due.isoformat() if entry.next_due else None,
                }
                for de, entry in self.vocab.items()
            },
            "mistakes": [asdict(m) for m in self.mistakes],
            "streak": {
                **asdict(self.streak),
                "last_day": self.streak.last_day.isoformat() if self.streak.last_day else None,
            },
            "totals": asdict(self.totals),
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # -- daily streak ---------------------------------------------------

    def touch_day(self, today: date | None = None) -> bool:
        """Count today as a practice day. Returns True on a new day."""
        today = today or date.today()
        if self.streak.last_day == today:
            return False
        if self.streak.last_day == today - timedelta(days=1):
            self.streak.current += 1
        else:
            self.streak.current = 1
        self.streak.longest = max(self.streak.longest, self.streak.current)
        self.streak.last_day = today
        return True

    # -- chat bookkeeping -------------------------------------------------

    def add_message(self) -> None:
        self.totals.messages += 1

    def add_mistake(self, example: str, fix: str, pattern: str = "") -> bool:
        """Log a correction event. Returns True when this (example→fix) pair
        is new; repeats only bump the count (keeps fehler.md deduplicated)."""
        now = date.today().isoformat()
        for m in self.mistakes:
            if m.example == example and m.fix == fix:
                m.count += 1
                m.last_seen = now
                self.totals.corrections += 1
                return False
        self.mistakes.append(
            Mistake(example=example, fix=fix, pattern=pattern, first_seen=now, last_seen=now)
        )
        self.totals.corrections += 1
        return True

    # -- SRS (SM-2-lite) --------------------------------------------------

    def apply_review(self, de: str, correct: bool, today: date | None = None) -> VocabEntry:
        """Apply one quiz answer: hit levels up (cap 5), miss drops a level
        (floor 1) and counts it; the new level's interval sets next_due."""
        entry = self.vocab[de]
        today = today or date.today()
        if correct:
            entry.level = min(MAX_LEVEL, entry.level + 1)
        else:
            entry.level = max(1, entry.level - 1)
            entry.misses += 1
        entry.next_due = today + timedelta(days=INTERVALS_DAYS[entry.level])
        return entry

    def due(self, today: date | None = None) -> list[str]:
        today = today or date.today()
        return [
            de
            for de, entry in self.vocab.items()
            if entry.next_due is None or entry.next_due <= today
        ]


@dataclass
class SessionState:
    """session.json — light session/schedule state (ONE state path, second file)."""

    last_session_end: str = ""
    last_session_turns: int = 0
    current_topic: str = ""
    pause_until: date | None = None
    path: Path = field(default_factory=lambda: Path("state") / "session.json")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "last_session_end": self.last_session_end,
            "last_session_turns": self.last_session_turns,
            "current_topic": self.current_topic,
            "pause_until": self.pause_until.isoformat() if self.pause_until else None,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    @classmethod
    def load(cls, path: Path) -> "SessionState":
        state = cls(path=Path(path))
        if not path.exists():
            return state
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            state.last_session_end = raw.get("last_session_end", "")
            state.last_session_turns = int(raw.get("last_session_turns", 0))
            state.current_topic = raw.get("current_topic", "")
            state.pause_until = _parse_date(raw.get("pause_until"))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return state
