"""state/memory.md — the agent's journal (v0.5, SPEC §4.6).

Third file in the ONE state path, host-only, never mounted into the
sandbox. The agent itself maintains it (read-modify-write, atomic save,
self-healing like srs.json): profile facts, which topics landed vs
flopped, phrases the learner reports using successfully, free-form notes.

It tailors phrasing, topics and examples ONLY — it never becomes a quiz
vocabulary source (§4.3 stands: quiz vocab = seed CSV + srs.json).

File format (parsed leniently; unparseable lines survive a save):

    # Herr Claw — Gedächtnis

    ## Profil
    - Arbeit: Softwareentwickler

    ## Interessen
    - Bäckerei — gelandet (3×)
    - Fußball — gefloppt (1×)

    ## Erfolgreiche Phrasen
    - „Ich hätte gern zwei Brötchen“ · Beim Bäcker · 2026-10-02 · 2×

    ## Notizen
    - mag Beispiele mit dem Namen seiner Schwester
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

LANDING_HIT = "gelandet"  # topic engaged the learner
LANDING_MISS = "gekippt"  # topic was skipped
GELANDET_PREFIX = "gelandet"
GEKIPPT_PREFIX = "gekippt"

SECTIONS = ("Profil", "Interessen", "Erfolgreiche Phrasen", "Notizen")

DEFAULT_TEXT = """\
# Herr Claw — Gedächtnis

## Profil

## Interessen

## Erfolgreiche Phrasen

## Notizen
"""

_QUOTE_CHARS = "„“\"»«'`"


def _clean(text: str) -> str:
    return text.strip().strip(_QUOTE_CHARS).strip()


@dataclass
class PhraseSuccess:
    phrase: str
    context: str = ""
    date: str = ""  # ISO day of the last reported use
    count: int = 1


@dataclass
class Interest:
    topic: str
    landing: str = LANDING_HIT  # gelandet | gekippt
    count: int = 1


@dataclass
class MemoryState:
    """In-memory image of state/memory.md. save() is atomic (tmp + replace);
    a file that cannot be read/decoded is backed up beside the original and
    replaced by a fresh journal — never silently dropped."""

    path: Path
    profil: list[str] = field(default_factory=list)
    interessen: list[Interest] = field(default_factory=list)
    erfolge: list[PhraseSuccess] = field(default_factory=list)
    notizen: list[str] = field(default_factory=list)
    _raw: dict[str, list[str]] = field(default_factory=dict)  # unparseable lines

    # -- persistence ------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "MemoryState":
        memory = cls(path=Path(path))
        if not memory.path.exists():
            return memory
        try:
            text = memory.path.read_text(encoding="utf-8")
        except (OSError, ValueError):  # unreadable or undecodable — quarantine
            memory._quarantine()
            return memory
        memory._parse(text)
        return memory

    def _quarantine(self) -> None:
        backup = self.path.with_suffix(f".md.corrupt-{int(time.time())}")
        try:
            self.path.rename(backup)
        except OSError:
            pass

    def _parse(self, text: str) -> None:
        section: str | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                heading = line[3:].strip()
                section = next((s for s in SECTIONS if s.lower() == heading.lower()), None)
                continue
            if section is None or not line or line.startswith("#"):
                continue
            content = line[2:].strip() if line.startswith("- ") else line
            if section == "Profil":
                self.profil.append(content)
            elif section == "Notizen":
                self.notizen.append(content)
            elif section == "Interessen":
                interest = _parse_interest(content)
                if interest:
                    self.interessen.append(interest)
                else:
                    self._raw.setdefault(section, []).append(line)
            elif section == "Erfolgreiche Phrasen":
                success = _parse_success(content)
                if success:
                    self.erfolge.append(success)
                else:
                    self._raw.setdefault(section, []).append(line)

    def render(self) -> str:
        lines = ["# Herr Claw — Gedächtnis", ""]
        for section, entries in (
            ("Profil", [f"- {item}" for item in self.profil]),
            ("Interessen", [f"- {format_interest(i)}" for i in self.interessen]),
            ("Erfolgreiche Phrasen", [f"- {format_success(s)}" for s in self.erfolge]),
            ("Notizen", [f"- {item}" for item in self.notizen]),
        ):
            lines.append(f"## {section}")
            lines.extend(entries)
            lines.extend(f"- {raw}" for raw in self._raw.get(section, []))
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".md.tmp")
        tmp.write_text(self.render(), encoding="utf-8")
        tmp.replace(self.path)

    # -- updates ------------------------------------------------------------

    def record_success(self, phrase: str, context: str = "", today: str = "") -> bool:
        """One reported use of a phrase. Returns True when the phrase is new.
        Repeats bump the count and move the date; empty context keeps the old."""
        phrase = _clean(phrase)
        if not phrase:
            return False
        today = today or time.strftime("%Y-%m-%d")
        for success in self.erfolge:
            if success.phrase.casefold() == phrase.casefold():
                success.count += 1
                success.date = today
                if context:
                    success.context = context
                return False
        self.erfolge.append(PhraseSuccess(phrase=phrase, context=context, date=today))
        return True

    def record_interest(self, topic: str, landed: bool) -> None:
        topic = _clean(topic)
        if not topic:
            return
        landing = LANDING_HIT if landed else LANDING_MISS
        for interest in self.interessen:
            if interest.topic.casefold() == topic.casefold():
                interest.landing = landing
                interest.count += 1
                return
        self.interessen.append(Interest(topic=topic, landing=landing))

    def add_note(self, note: str) -> None:
        note = _clean(note)
        if note and note.casefold() not in {n.casefold() for n in self.notizen}:
            self.notizen.append(note)

    def landed_topics(self) -> list[str]:
        return [i.topic for i in self.interessen if i.landing == LANDING_HIT]

    # -- prompt use ------------------------------------------------------------

    def prompt_block(self, max_phrases: int = 5, max_chars: int = 700) -> str:
        """Compact context for content-generating prompts (chat/sprechen
        session start, Morgen-Brief, /tag). Phrasing/topics/examples only —
        the quiz word list never comes from here."""
        lines: list[str] = []
        if self.profil:
            lines.append("Profil: " + "; ".join(self.profil)[:200])
        if self.interessen:
            liked = [f"{i.topic} ({i.count}×)" for i in self.interessen if i.landing == LANDING_HIT]
            skipped = [i.topic for i in self.interessen if i.landing == LANDING_MISS]
            if liked:
                lines.append("Interessen, die ankamen: " + ", ".join(liked))
            if skipped:
                lines.append("Interessen, die flopten (meiden): " + ", ".join(skipped))
        if self.erfolge:
            # most-reinforced first; ties keep the newest reported phrase
            best = sorted(list(reversed(self.erfolge)), key=lambda s: -s.count)[:max_phrases]
            lines.append("Erfolgreiche Phrasen: " + " · ".join(f"„{s.phrase}“ ({s.count}×)" for s in best))
        if self.notizen:
            lines.append("Notizen: " + "; ".join(self.notizen[:3]))
        if not lines:
            return ""
        block = "[Gedächtnis — passe Beispiele und Themen daran an, erfinde nichts]\n" + "\n".join(lines)
        return block[:max_chars]


def format_interest(interest: Interest) -> str:
    return f"{interest.topic} — {interest.landing} ({interest.count}×)"


def format_success(success: PhraseSuccess) -> str:
    context = success.context or "-"
    return f"„{success.phrase}“ · {context} · {success.date or '-'} · {success.count}×"


def _parse_interest(content: str) -> Interest | None:
    # "Bäckerei — gelandet (3×)"
    if "—" not in content:
        return None
    topic, _, rest = content.partition("—")
    landing = LANDING_HIT if rest.strip().lower().startswith(GELANDET_PREFIX) else LANDING_MISS
    count = 1
    if "(" in rest and "×" in rest:
        try:
            count = int(rest[rest.rindex("(") + 1 : rest.rindex("×")].strip())
        except ValueError:
            count = 1
    topic = _clean(topic)
    if not topic:
        return None
    return Interest(topic=topic, landing=landing, count=max(1, count))


def _parse_success(content: str) -> PhraseSuccess | None:
    # "„Ich hätte gern zwei Brötchen“ · Beim Bäcker · 2026-10-02 · 2×"
    parts = [part.strip() for part in content.split("·")]
    if len(parts) < 2:
        return None
    phrase = _clean(parts[0])
    if not phrase:
        return None
    context = _clean(parts[1])
    date = parts[2].strip() if len(parts) > 2 else ""
    if not date or not date[0].isdigit():
        date = ""
    count = 1
    if len(parts) > 3:
        digits = "".join(ch for ch in parts[3] if ch.isdigit())
        count = int(digits) if digits else 1
    return PhraseSuccess(phrase=phrase, context=context, date=date, count=max(1, count))
