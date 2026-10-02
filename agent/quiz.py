"""The /quiz engine (P4, SPEC §4.3) — SRS drills constrained to known vocab.

Anti-hallucination rule (AGENTS.md): quiz content stays inside
seed/vocab_a1_100.csv + words already in state/srs.json — questions are
generated from stored data only, never by the LLM. ensure_seeded() imports
the seed list into the SRS once; existing progress is never overwritten.
Mass nouns without a common A1 plural (das Wasser, die Milch, …) carry no
plural — they are shown with the article only.

Question types (SPEC §4.3): DE→EN, EN→DE, cloze („Ich esse ___“), article
(der/die/das), plural. Nouns always appear with article + plural.
"""

from __future__ import annotations

import csv
import random
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .state import SrsState, VocabEntry

SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "vocab_a1_100.csv"

QUIZ_DEFAULT = 5
QUIZ_MAX = 20

DE_EN = "de_en"
EN_DE = "en_de"
CLOZE = "cloze"
ARTICLE = "article"
PLURAL = "plural"

_ARTICLE_RE = re.compile(r"^(der|die|das)\s+", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]|_")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

QUIT_WORDS = ("ende", "fertig", "stopp", "stop", "q", "quit")


def normalize(text: str) -> str:
    """Lenient learner answer matching: case-, umlaut- (ae/oe/ue/ss) and
    punctuation-insensitive; collapsed whitespace."""
    text = text.strip().lower().translate(_UMLAUTS)
    text = _PUNCT_RE.sub(" ", text)
    return " ".join(text.split())


def base_word(de: str) -> str:
    """'das Brot' → 'Brot' (article quizzes show the bare noun)."""
    return _ARTICLE_RE.sub("", de.strip())


def load_seed(path: Path = SEED_PATH) -> dict[str, dict]:
    """seed/vocab_a1_100.csv → {de: {en, article, plural, theme, example}}."""
    rows: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            de = (row["de"] or "").strip()
            if de:
                rows[de] = {
                    "en": (row["en"] or "").strip(),
                    "article": (row["article"] or "").strip(),
                    "plural": (row["plural"] or "").strip(),
                    "theme": (row["theme"] or "").strip(),
                    "example": (row["example"] or "").strip(),
                }
    return rows


def ensure_seeded(srs: SrsState, path: Path = SEED_PATH) -> int:
    """Import missing seed words (level 1, due immediately). Words already in
    the SRS keep their progress. Returns the number added; saves only then."""
    added = 0
    for de, meta in load_seed(path).items():
        if de in srs.vocab:
            continue
        srs.vocab[de] = VocabEntry(
            en=meta["en"], article=meta["article"], plural=meta["plural"]
        )
        added += 1
    if added:
        srs.save()
    return added


def weakest_words(srs: SrsState, n: int, rng: random.Random | None = None) -> list[str]:
    """Lowest level first, then most misses, then earliest due — the pool for
    /quiz and the 12:30 micro-quiz. A small random sample from the weakest
    band keeps consecutive quizzes from repeating identical questions."""
    ranked = [
        de
        for de, entry in sorted(
            srs.vocab.items(),
            key=lambda item: (
                item[1].level,
                -item[1].misses,
                item[1].next_due or date.min,
                item[0],
            ),
        )
    ]
    head = ranked[: max(n * 2, n)]
    if rng is None:
        return head[:n]
    return rng.sample(head, min(n, len(head)))


@dataclass(frozen=True)
class QuizQuestion:
    key: str  # SRS key, e.g. "das Brot"
    qtype: str
    prompt: str
    display: str  # shown on a miss: "das Brot (die Brote)"
    accepted: tuple[str, ...]

    def check(self, answer: str) -> bool:
        want = {normalize(v) for v in self.accepted}
        return normalize(answer) in want


def word_display(key: str, entry: VocabEntry) -> str:
    """Nouns with article + plural (AGENTS.md): 'das Brot (die Brote)'."""
    if entry.article and entry.plural:
        return f"{key} ({entry.plural})"
    return key


def _accepted_for(qtype: str, key: str, entry: VocabEntry) -> tuple[str, ...]:
    if qtype == DE_EN:
        return (entry.en, entry.en.removeprefix("to ").strip())
    if qtype == EN_DE:
        return (key, base_word(key)) if entry.article else (key,)
    if qtype == CLOZE:
        if entry.article:
            plural = entry.plural
            return (base_word(key), key, plural, base_word(plural) if plural else plural)
        return (key,)
    if qtype == ARTICLE:
        return (entry.article,)
    return (entry.plural, base_word(entry.plural))  # PLURAL


def make_question(
    key: str,
    entry: VocabEntry,
    example: str = "",
    rng: random.Random | None = None,
    qtype: str | None = None,
) -> QuizQuestion:
    """Build one question from stored data only. `example` (seed cloze
    sentence with '___') enables the CLOZE type; article/plural types apply
    to nouns only."""
    options = [DE_EN, EN_DE]
    if example and "___" in example:
        options.append(CLOZE)
    if entry.article:
        options.append(ARTICLE)
    if entry.plural:
        options.append(PLURAL)
    qtype = qtype if qtype in options else (rng.choice(options) if rng else options[0])

    if qtype == DE_EN:
        prompt = f"Was heißt „{key}“ auf Englisch?"
    elif qtype == EN_DE:
        prompt = f"Wie sagt man „{entry.en}“ auf Deutsch?"
    elif qtype == CLOZE:
        prompt = f"Setze ein: „{example}“ (englisch: {entry.en})"
    elif qtype == ARTICLE:
        prompt = f"der, die oder das? „___ {base_word(key)}“"
    else:  # PLURAL
        prompt = f"Was ist der Plural von „{key}“?"
    return QuizQuestion(
        key=key,
        qtype=qtype,
        prompt=prompt,
        display=word_display(key, entry),
        accepted=_accepted_for(qtype, key, entry),
    )


class QuizSession:
    """One running quiz. Lives on the ChatContext so chat, sprechen and
    Telegram all grade answers identically: plain text while a quiz is
    active is an answer, a quit word ends it early."""

    def __init__(
        self,
        srs: SrsState,
        questions: list[QuizQuestion],
        today: date | None = None,
    ) -> None:
        self.srs = srs
        self.questions = questions
        self.today = today or date.today()
        self.index = 0
        self.correct = 0

    @property
    def finished(self) -> bool:
        return self.index >= len(self.questions)

    def intro(self) -> str:
        first = self.questions[0]
        return f"Frage 1/{len(self.questions)}: {first.prompt}"

    def answer(self, text: str) -> str:
        question = self.questions[self.index]
        ok = question.check(text)
        self.srs.apply_review(question.key, ok, today=self.today)
        self.index += 1
        if ok:
            self.correct += 1
        feedback = "✅ Richtig!" if ok else f"❌ Leider falsch. Richtig: {question.display}"
        if self.finished:
            return f"{feedback}\n\n{self.summary()}"
        nxt = self.questions[self.index]
        return f"{feedback}\n\nFrage {self.index + 1}/{len(self.questions)}: {nxt.prompt}"

    def cancel(self) -> str:
        self.index = len(self.questions)
        return f"Quiz beendet ({self.correct}/{len(self.questions)} richtig). Bis zum nächsten Mal!"

    def summary(self) -> str:
        total = len(self.questions)
        text = f"🎓 Quiz beendet: {self.correct}/{total} richtig."
        if total and self.correct == total:
            text += " Fantastisch! 🌟"
        elif self.correct == 0:
            text += " Kein Problem — die Wörter kommen bald wieder."
        return text

    def to_json(self) -> dict:
        """Persist the running quiz (session.json) so a quiz started in one
        process — the cron quiz fire, a TUI session — can be answered in
        another (the Telegram poller, the next `turn`)."""
        return {
            "questions": [
                {
                    "key": q.key,
                    "qtype": q.qtype,
                    "prompt": q.prompt,
                    "display": q.display,
                    "accepted": list(q.accepted),
                }
                for q in self.questions
            ],
            "index": self.index,
            "correct": self.correct,
            "today": self.today.isoformat(),
        }

    @classmethod
    def from_json(cls, srs: SrsState, data: dict) -> "QuizSession | None":
        """Rebuild a persisted quiz; None when the payload is malformed."""
        try:
            questions = [
                QuizQuestion(
                    key=str(q["key"]),
                    qtype=str(q["qtype"]),
                    prompt=str(q["prompt"]),
                    display=str(q["display"]),
                    accepted=tuple(str(a) for a in q["accepted"]),
                )
                for q in data["questions"]
            ]
            if not questions:
                return None
            session = cls(srs, questions, today=date.fromisoformat(str(data["today"])))
            session.index = int(data["index"])
            session.correct = int(data["correct"])
            return session
        except (KeyError, TypeError, ValueError):
            return None


def start(
    srs: SrsState,
    n: int = QUIZ_DEFAULT,
    rng: random.Random | None = None,
    today: date | None = None,
    path: Path = SEED_PATH,
) -> QuizSession:
    """Seed the SRS if needed, then build an n-question quiz from the weakest
    words. Both /quiz and the 12:30 micro-quiz land here."""
    seed = load_seed(path)
    if any(de not in srs.vocab for de in seed):
        ensure_seeded(srs, path)
    rng = rng or random.Random()
    n = max(1, min(QUIZ_MAX, n))
    questions = [
        make_question(key, srs.vocab[key], example=seed.get(key, {}).get("example", ""), rng=rng)
        for key in weakest_words(srs, n, rng)
    ]
    return QuizSession(srs, questions, today=today)
