"""The Morgen-Brief builder (v0.5, SPEC §4.6) — 5–8 German phrases for the
actual day, composed from REAL data only:

- every calendar event whose title maps to a theme (TOPIC_KEYWORDS, same
  table the P2 topic flourish uses) contributes 1–2 usable phrases from a
  fixed A1–A2 bank — never an invented calendar connection;
- due SRS vocab is woven in via the seed CSV's cloze examples (___ → the
  word with article), so the phrases practise exactly what is due;
- memory interests pick one extra phrase's flavor (topics that landed).

Pure functions over plain data — the tests never touch Apple, the bridge
or the LLM. Deterministic given the injected rng. Output is Telegram- and
TTS-safe: numbered lines, no markdown tables.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import quiz as quiz_mod
from .memory import MemoryState
from .scheduling import EVENT_TITLE, TOPIC_KEYWORDS
from .state import SrsState

DEFAULT_TOPIC = "Smalltalk und Alltag"
PLAN_MIN = 5
PLAN_MAX = 8
EVENT_THEMES_MAX = 2  # mapped events that contribute phrases
EVENT_PHRASES_MAX = 4  # cap across all events
VOCAB_PHRASES_MAX = 3

# Fixed A1–A2 phrase bank per practice theme. Nouns with article (AGENTS.md);
# every TOPIC_KEYWORDS topic, every seed theme and the DEFAULT_TOPIC is keyed.
PHRASE_BANK: dict[str, tuple[str, ...]] = {
    "Behörden und Termine": (
        "Ich habe einen Termin beim Bürgeramt.",
        "Können wir den Termin verschieben?",
        "Ich muss ein Formular ausfüllen.",
        "Wo bekomme ich den Antrag?",
        "Ich habe meinen Pass vergessen.",
    ),
    "Beim Bäcker": (
        "Ich hätte gern zwei Brötchen, bitte.",
        "Was kostet das Brot?",
        "Haben Sie etwas Süßes?",
        "Ich nehme noch ein Stück Kuchen.",
        "Das ist alles, danke!",
    ),
    "Einkaufen": (
        "Wo finde ich die Milch?",
        "Ein Kilo Äpfel, bitte.",
        "Zahle ich bar oder mit Karte?",
        "Brauche ich eine Tüte?",
        "Gibt es das auch günstiger?",
    ),
    "Beim Arzt": (
        "Ich habe Kopfschmerzen.",
        "Ich brauche einen Termin beim Arzt.",
        "Mein Bauch tut weh.",
        "Ich bin krank und kann nicht arbeiten.",
        "Brauche ich ein Rezept?",
    ),
    "Reisen": (
        "Wann fährt der nächste Zug?",
        "Eine Fahrkarte nach Berlin, bitte.",
        "Wo ist der Bahnhof?",
        "Der Flug hat Verspätung.",
        "Ich nehme den Bus zur Arbeit.",
    ),
    "Sport": (
        "Wir spielen heute Fußball.",
        "Ich fahre gern mit dem Fahrrad.",
        "Machst du gern Yoga?",
        "Ich gehe zweimal pro Woche ins Fitnessstudio.",
        "Sport macht müde, aber glücklich.",
    ),
    "Essen gehen": (
        "Einen Tisch für zwei, bitte.",
        "Was empfehlen Sie?",
        "Ich nehme die Suppe als Vorspeise.",
        "Die Rechnung, bitte!",
        "Das Essen war sehr lecker.",
    ),
    "Arbeit und Büro": (
        "Um 14 Uhr habe ich ein Meeting.",
        "Können wir das Meeting verschieben?",
        "Ich schicke Ihnen die Unterlagen.",
        "Ich arbeite heute von zu Hause.",
        "Ich habe morgen einen Termin mit dem Chef.",
    ),
    "Wohnung": (
        "Ich suche eine Wohnung in Berlin.",
        "Die Miete ist zu hoch.",
        "Die Küche ist klein, aber schön.",
        "Ich ziehe nächsten Monat um.",
        "Wann kann ich die Wohnung besichtigen?",
    ),
    "Essen & Trinken": (
        "Zum Frühstück esse ich ein Ei.",
        "Ich trinke meinen Kaffee ohne Zucker.",
        "Ich koche heute Nudeln mit Soße.",
        "Möchtest du ein Glas Wasser?",
        "Das Abendessen schmeckt gut.",
    ),
    "Smalltalk": (
        "Wie war dein Wochenende?",
        "Das Wetter ist heute schön.",
        "Was machst du in deiner Freizeit?",
        "Ich lerne gerade Deutsch.",
        "Am Wochenende treffe ich Freunde.",
    ),
    "Berlin & Alltag": (
        "Ich fahre mit der U-Bahn zur Arbeit.",
        "Der Bus kommt in fünf Minuten.",
        "Ich wohne im dritten Stock.",
        "Am Samstag gehe ich auf den Markt.",
        "In Berlin gibt es viele Parks.",
    ),
    "Smalltalk und Alltag": (
        "Wie war dein Tag?",
        "Ich war heute viel unterwegs.",
        "Das Wetter wird besser.",
        "Am Abend lese ich ein Buch.",
        "Was machst du morgen?",
    ),
}

# Interest/memory topics → themes (extends TOPIC_KEYWORDS; ordered, first hit).
INTEREST_KEYWORDS: tuple[tuple[str, str], ...] = TOPIC_KEYWORDS + (
    ("brot", "Essen & Trinken"),
    ("brötchen", "Essen & Trinken"),
    ("kuchen", "Essen & Trinken"),
    ("kaffee", "Essen & Trinken"),
    ("frühstück", "Essen & Trinken"),
    ("kochen", "Essen & Trinken"),
    ("fußball", "Sport"),
    ("fahrrad", "Sport"),
    ("musik", "Smalltalk"),
    ("film", "Smalltalk"),
    ("freunde", "Smalltalk"),
    ("familie", "Smalltalk"),
    ("markt", "Berlin & Alltag"),
    ("u-bahn", "Berlin & Alltag"),
)


@dataclass
class DayPlan:
    """Today's tailored phrase plan — stored in session.json (`day_plan`)
    so /tag re-shows exactly what the morning push sent."""

    date: str
    topic: str
    phrases: list[str] = field(default_factory=list)
    source: str = "fallback"  # "calendar" | "fallback"
    events: list[tuple[str, str]] = field(default_factory=list)  # (HH:MM, title)

    def to_json(self) -> dict:
        return {
            "date": self.date,
            "topic": self.topic,
            "phrases": list(self.phrases),
            "source": self.source,
            "events": [[when, title] for when, title in self.events],
        }

    @classmethod
    def from_json(cls, raw: dict | None) -> "DayPlan | None":
        if not isinstance(raw, dict):
            return None
        phrases = raw.get("phrases")
        if not isinstance(phrases, list):
            return None
        events = [
            (str(when), str(title))
            for when, title in (raw.get("events") or [])
            if isinstance(when, str) and isinstance(title, str)
        ]
        return cls(
            date=str(raw.get("date", "")),
            topic=str(raw.get("topic", "")),
            phrases=[str(p) for p in phrases],
            source=str(raw.get("source", "fallback")),
            events=events,
        )


def theme_for_text(text: str) -> str | None:
    """Keyword → theme (TOPIC_KEYWORDS first, then the interest table)."""
    lowered = text.lower()
    return next((t for kw, t in INTEREST_KEYWORDS if kw in lowered), None)


def phrase_vocab_hits(norm_text: str, key: str) -> bool:
    """Word-boundary check: does the SRS key (or its bare noun) occur in an
    already-normalized phrase? 'brötchen' must not hit 'brot' — the match is
    on whole words after the quiz-normalizer (umlaut/punctuation-lenient)."""
    candidates = {quiz_mod.normalize(key)}
    bare = quiz_mod.normalize(quiz_mod.base_word(key))
    if bare:
        candidates.add(bare)
    return any(
        re.search(rf"(?:^| ){re.escape(candidate)}(?: |$)", norm_text)
        for candidate in candidates
        if candidate
    )


def fallback_topic(srs: SrsState) -> str:
    """Theme of the weakest vocab — the honest non-calendar topic."""
    from .quiz import load_seed, weakest_words

    weakest = weakest_words(srs, 1)
    if weakest:
        meta = load_seed().get(weakest[0], {})
        if meta.get("theme"):
            return meta["theme"]
    return DEFAULT_TOPIC


def _pick(rng: random.Random | None, bank: tuple[str, ...], n: int) -> list[str]:
    if rng is None:
        return list(bank[:n])
    return rng.sample(bank, min(n, len(bank)))


def build_day_plan(
    *,
    events: list[dict],
    now: datetime,
    srs: SrsState,
    memory: MemoryState | None = None,
    seed: dict[str, dict] | None = None,
    rng: random.Random | None = None,
    topic: str | None = None,
) -> DayPlan:
    """Compose today's plan. `topic` (e.g. from suggest_topic) wins; else the
    first mapped event's theme; else the weakest-vocab theme. source records
    honestly whether the calendar contributed anything."""
    from .quiz import load_seed

    seed = seed if seed is not None else load_seed()
    phrases: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            phrases.append(candidate)

    # 1) real calendar events → theme phrases (upcoming first)
    plan_events: list[tuple[str, str]] = []
    event_themes: list[str] = []
    for event in sorted(
        (e for e in events if e.get("title") and e.get("start")),
        key=lambda e: str(e["start"]),
    ):
        title = str(event["title"])
        if title.strip() == EVENT_TITLE:
            continue  # our own booked practice slot is not a "real life" event
        theme = theme_for_text(title)
        if theme is None:
            continue
        when = event["start"][11:16] if len(str(event["start"])) >= 16 and not event.get("all_day") else ""
        plan_events.append((when, title))
        if theme not in event_themes and len(event_themes) < EVENT_THEMES_MAX:
            event_themes.append(theme)
    for theme in event_themes:
        for phrase in _pick(rng, PHRASE_BANK.get(theme, ()), 2)[: max(0, EVENT_PHRASES_MAX - (len(phrases)))]:
            add(phrase)

    # 2) due SRS vocab woven in via the seed cloze examples
    due = [de for de in quiz_mod.weakest_words(srs, 6) if seed.get(de, {}).get("example", "").count("___") == 1]
    for de in due[:VOCAB_PHRASES_MAX]:
        add(seed[de]["example"].replace("___", de))

    # 3) memory interests pick the flavor
    if memory is not None:
        for interest_topic in memory.landed_topics():
            theme = theme_for_text(interest_topic)
            if theme and PHRASE_BANK.get(theme):
                add(_pick(rng, PHRASE_BANK[theme], 1)[0])
                break

    # 4) resolve the topic, then pad to PLAN_MIN from its bank
    if topic is None:
        topic = event_themes[0] if event_themes else fallback_topic(srs)
    pad_bank = PHRASE_BANK.get(topic) or PHRASE_BANK[DEFAULT_TOPIC]
    for phrase in _pick(rng, pad_bank, PLAN_MIN):
        if len(phrases) >= PLAN_MIN:
            break
        add(phrase)
    for phrase in _pick(rng, PHRASE_BANK[DEFAULT_TOPIC], PLAN_MIN):
        if len(phrases) >= PLAN_MIN:
            break
        add(phrase)

    source = "calendar" if event_themes else "fallback"
    return DayPlan(
        date=now.date().isoformat(),
        topic=topic,
        phrases=phrases[:PLAN_MAX],
        source=source,
        events=plan_events,
    )


def render_plan_lines(plan: DayPlan) -> str:
    return "\n".join(f"{i}. {phrase}" for i, phrase in enumerate(plan.phrases, start=1))


def render_brief(plan: DayPlan, *, booking: str = "", closing: str = "") -> str:
    """The Telegram push (SPEC §4.1): greeting + topic + the phrase list."""
    lines = [f"Guten Morgen! ☕ Bereit für 5 Minuten? Heute: „{plan.topic}“."]
    if plan.events:
        shown = " · ".join(f"{when} {title}".strip() for when, title in plan.events[:3])
        lines.append(f"Heute im Kalender: {shown}")
    lines.append("")
    lines.append("Deine Phrasen für heute:")
    lines.append(render_plan_lines(plan))
    if plan.source == "fallback":
        lines.append("")
        lines.append("ℹ︎ Kein Kalender gelesen — die Phrasen kommen aus deinen Vokabeln und Interessen.")
    if booking:
        lines.append("")
        lines.append(booking)
    if closing:
        lines.append("")
        lines.append(closing)
    return "\n".join(lines)


def render_tag(plan: DayPlan) -> str:
    """/tag — re-show today's plan (same content the morning push sent)."""
    lines = [f"📋 Dein Tag — Thema: „{plan.topic}“", ""]
    lines.append(render_plan_lines(plan))
    lines.append("")
    quelle = "aus deinem Kalender" if plan.source == "calendar" else "aus deinen Vokabeln und Interessen (kein Kalender gelesen)"
    lines.append(f"Quelle: {quelle}.")
    lines.append("Erzähl mir am Abend, welche du benutzt hast: /erfolge <Satz>")
    return "\n".join(lines)
