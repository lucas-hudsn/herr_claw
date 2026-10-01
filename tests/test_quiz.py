"""agent/quiz.py — seed import, question types, answer checking, SRS updates."""

import random
from datetime import date

import pytest

from agent.quiz import (
    ARTICLE,
    CLOZE,
    DE_EN,
    EN_DE,
    PLURAL,
    QUIZ_MAX,
    QuizQuestion,
    QuizSession,
    base_word,
    ensure_seeded,
    load_seed,
    make_question,
    normalize,
    start,
    weakest_words,
)
from agent.state import VocabEntry

BROT = VocabEntry(en="bread", article="das", plural="die Brote")


# ---- seed file (repo content — the anti-hallucination contract) --------------


class TestSeedFile:
    def test_exactly_100_unique_words(self):
        seed = load_seed()
        assert len(seed) == 100

    def test_nouns_carry_article_from_der_die_das(self):
        for de, meta in load_seed().items():
            assert meta["article"] in ("der", "die", "das", "")

    def test_nouns_with_plural_show_article_plus_plural(self):
        for de, meta in load_seed().items():
            if meta["plural"]:
                assert de.startswith(meta["article"] + " ")
                assert meta["plural"].startswith("die ") or meta["plural"].startswith(
                    "der "
                ) or meta["plural"].startswith("das ")

    def test_cloze_examples_mask_exactly_once(self):
        for meta in load_seed().values():
            if meta["example"]:
                assert meta["example"].count("___") == 1

    def test_themes_are_the_spec_domains(self):
        themes = {meta["theme"] for meta in load_seed().values()}
        assert themes == {"Essen & Trinken", "Wohnung", "Smalltalk", "Berlin & Alltag"}


# ---- ensure_seeded ------------------------------------------------------------


def test_ensure_seeded_imports_all_and_is_idempotent(srs):
    assert ensure_seeded(srs) == 100
    assert len(srs.vocab) == 100
    assert srs.vocab["das Brot"].en == "bread"
    assert ensure_seeded(srs) == 0  # second import is a no-op
    assert len(srs.vocab) == 100


def test_ensure_seeded_never_overwrites_progress(srs):
    srs.vocab["das Brot"] = VocabEntry(en="bread", article="das", plural="die Brote", level=4)
    ensure_seeded(srs)
    assert srs.vocab["das Brot"].level == 4
    assert len(srs.vocab) == 100  # the other 99 were added


# ---- normalization / checking ---------------------------------------------------


def test_normalize_handles_case_umlauts_and_punctuation():
    assert normalize("  Die Brote!") == normalize("die brote")
    assert normalize("Äpfel") == normalize("aepfel")
    assert normalize("Grüße") == normalize("gruesse")
    assert normalize("das Brot.") == "das brot"


def test_base_word_strips_article():
    assert base_word("das Brot") == "Brot"
    assert base_word("trinken") == "trinken"


def make(qtype, entry=BROT, example="Ich esse ___ mit Käse."):
    return make_question("das Brot", entry, example=example, qtype=qtype)


class TestQuestionTypes:
    def test_de_en(self):
        q = make(DE_EN)
        assert q.prompt == "Was heißt „das Brot“ auf Englisch?"
        assert q.check("bread") and q.check("  Bread! ")
        assert not q.check("Brot")

    def test_de_en_accepts_verb_without_to(self):
        trinken = VocabEntry(en="to drink")
        q = make_question("trinken", trinken, qtype=DE_EN)
        assert q.check("drink") and q.check("to drink")

    def test_en_de(self):
        q = make(EN_DE)
        assert q.prompt == "Wie sagt man „bread“ auf Deutsch?"
        assert q.check("das Brot") and q.check("brot")
        assert not q.check("bread")

    def test_cloze_shows_sentence_with_gloss(self):
        q = make(CLOZE)
        assert "___" in q.prompt and "englisch: bread" in q.prompt
        assert q.check("Brot") and q.check("das Brot")
        assert not q.check("Butter")

    def test_article(self):
        q = make(ARTICLE)
        assert q.prompt == "der, die oder das? „___ Brot“"
        assert q.check("das") and q.check("Das!")
        assert not q.check("die")

    def test_plural(self):
        q = make(PLURAL)
        assert q.prompt == "Was ist der Plural von „das Brot“?"
        assert q.check("die Brote") and q.check("brote")
        assert not q.check("das Brot")

    def test_mass_noun_has_no_article_or_plural_quiz(self):
        wasser = VocabEntry(en="water", article="das", plural="")
        q = make_question("das Wasser", wasser, qtype=ARTICLE)
        # forced type still works; but it is never chosen randomly
        assert q.check("das")

    def test_random_type_only_from_applicable(self):
        wasser = VocabEntry(en="water", article="das", plural="")
        rng = random.Random(7)
        for _ in range(30):
            q = make_question("das Wasser", wasser, rng=rng)
            assert q.qtype in (DE_EN, EN_DE, ARTICLE)


# ---- weakest-first selection ----------------------------------------------------


def test_weakest_words_lowest_level_then_most_misses(srs):
    srs.vocab = {
        "a": VocabEntry(en="a", level=3),
        "b": VocabEntry(en="b", level=1, misses=2),
        "c": VocabEntry(en="c", level=1),
        "d": VocabEntry(en="d", level=5),
    }
    assert weakest_words(srs, 2) == ["b", "c"]
    assert weakest_words(srs, 10) == ["b", "c", "a", "d"]


def test_weakest_words_random_sample_stays_in_weakest_band(srs):
    srs.vocab = {
        f"w{i}": VocabEntry(en=str(i), level=1 if i < 3 else 5) for i in range(10)
    }
    picks = weakest_words(srs, 3, rng=random.Random(1))
    assert len(picks) == 3 and len(set(picks)) == 3
    assert all(pick.startswith("w") and int(pick[1:]) < 6 for pick in picks)


# ---- session flow (SRS updates) ---------------------------------------------------


def test_quiz_session_flow_updates_srs(srs):
    ensure_seeded(srs)
    today = date(2026, 10, 2)
    questions = [
        make_question("das Brot", srs.vocab["das Brot"], qtype=DE_EN),
        make_question("trinken", srs.vocab["trinken"], qtype=EN_DE),
    ]
    session = QuizSession(srs, questions, today=today)
    assert "Frage 1/2" in session.intro()

    out = session.answer("bread")
    assert out.startswith("✅ Richtig!")
    assert "Frage 2/2" in out
    assert srs.vocab["das Brot"].level == 2  # hit → level up

    out = session.answer("something wrong")
    assert "❌ Leider falsch" in out
    assert "trinken" in out  # the expected display
    assert srs.vocab["trinken"].misses == 1  # miss → counted
    assert session.finished
    assert "0/2" not in out and "1/2 richtig" in out  # summary counts only hits


def test_quiz_session_cancel_and_summary(srs):
    session = QuizSession(
        srs, [make_question("das Brot", BROT, qtype=DE_EN)], today=date(2026, 10, 2)
    )
    assert "0/1 richtig" in session.cancel()
    assert session.finished


def test_start_seeds_and_builds_n_questions(srs):
    quiz = start(srs, n=3, today=date(2026, 10, 2))
    assert len(quiz.questions) == 3
    assert len(srs.vocab) == 100
    assert all(q.key in srs.vocab for q in quiz.questions)


def test_start_clamps_n(srs):
    assert len(start(srs, n=999).questions) == QUIZ_MAX
    assert len(start(srs, n=0).questions) == 1


def test_quiz_questions_only_from_srs_vocab(srs):
    # the anti-hallucination contract: every question key is known vocabulary
    quiz = start(srs, n=10)
    assert {q.key for q in quiz.questions} <= set(srs.vocab.keys())
