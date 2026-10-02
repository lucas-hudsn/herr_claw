"""agent/phrases.py — the Morgen-Brief builder (v0.5, SPEC §4.6).

Real events only, due vocab woven in, memory picks the flavor, 5–8
phrases, no markdown tables. Pure data in, DayPlan out.
"""

import json
import random
from pathlib import Path

from agent.memory import MemoryState
from agent.phrases import (
    DEFAULT_TOPIC,
    PHRASE_BANK,
    PLAN_MAX,
    PLAN_MIN,
    DayPlan,
    build_day_plan,
    fallback_topic,
    phrase_vocab_hits,
    render_brief,
    render_tag,
)
from agent.quiz import ensure_seeded, load_seed
from agent.state import SrsState, VocabEntry


def make_now(hour=8, minute=0):
    from datetime import datetime

    return datetime(2026, 10, 2, hour, minute)


def event(title, start="2026-10-02T16:00", end="2026-10-02T16:30"):
    return {"title": title, "start": start, "end": end, "all_day": False}


def test_event_mapped_to_theme_contributes_phrases(srs):
    ensure_seeded(srs)
    plan = build_day_plan(
        events=[event("Anmeldung Bürgeramt")], now=make_now(), srs=srs, rng=random.Random(1)
    )
    assert plan.source == "calendar"
    assert plan.topic == "Behörden und Termine"
    bank = set(PHRASE_BANK["Behörden und Termine"])
    assert any(p in bank for p in plan.phrases)
    assert plan.events == [("16:00", "Anmeldung Bürgeramt")]


def test_own_booking_is_not_a_real_life_event(srs):
    ensure_seeded(srs)
    plan = build_day_plan(
        events=[event("Deutsch Lernen (15m)")], now=make_now(), srs=srs, rng=random.Random(1)
    )
    assert plan.events == []  # our own practice slot never masquerades as calendar tailoring
    assert plan.source == "fallback"


def test_due_vocab_is_woven_in_via_seed_cloze(srs):
    ensure_seeded(srs)
    seed = load_seed()
    plan = build_day_plan(events=[], now=make_now(), srs=srs, rng=random.Random(3))
    filled = {meta["example"].replace("___", de) for de, meta in seed.items() if meta["example"].count("___") == 1}
    assert any(p in filled for p in plan.phrases)


def test_memory_interest_picks_the_flavor(srs, tmp_path):
    ensure_seeded(srs)
    memory = MemoryState.load(tmp_path / "memory.md")
    memory.record_interest("Bäckerei", landed=True)
    plan = build_day_plan(events=[], now=make_now(), srs=srs, memory=memory, rng=random.Random(5))
    bank = set(PHRASE_BANK["Beim Bäcker"])
    assert any(p in bank for p in plan.phrases)


def test_plan_stays_within_five_to_eight_phrases(srs):
    ensure_seeded(srs)
    busy = [
        event("Anmeldung Bürgeramt", "2026-10-02T09:00", "2026-10-02T09:30"),
        event("Meeting mit dem Team", "2026-10-02T11:00", "2026-10-02T11:30"),
        event("Beim Bäcker", "2026-10-02T13:00", "2026-10-02T13:15"),
    ]
    memory = MemoryState.load(Path("/nonexistent/memory.md"))
    for seed in range(20):
        plan = build_day_plan(events=busy, now=make_now(), srs=srs, memory=memory, rng=random.Random(seed))
        assert PLAN_MIN <= len(plan.phrases) <= PLAN_MAX
        assert len(set(plan.phrases)) == len(plan.phrases)  # no duplicates


def test_fallback_is_honest_when_nothing_maps(srs):
    ensure_seeded(srs)
    plan = build_day_plan(
        events=[event("Kaffee mit Lisa")],  # real event, maps via the interest table
        now=make_now(),
        srs=srs,
        rng=random.Random(2),
    )
    assert plan.source == "calendar"
    # a truly unmappable day falls back:
    plan2 = build_day_plan(events=[event("Xyzzz-Quatsch")], now=make_now(), srs=srs, rng=random.Random(2))
    assert plan2.source == "fallback"
    brief = render_brief(plan2)
    assert "Kein Kalender gelesen" in brief  # the message says so honestly


def test_fallback_topic_comes_from_weakest_vocab(srs):
    srs.vocab["der Zug"] = VocabEntry(en="train", level=1)  # the only word → the weakest
    assert fallback_topic(srs) == "Berlin & Alltag"
    empty = fallback_topic(SrsState(Path("/nonexistent/srs.json")))
    assert empty == DEFAULT_TOPIC


def test_render_brief_layout_is_tts_safe(srs):
    ensure_seeded(srs)
    plan = build_day_plan(events=[event("Anmeldung Bürgeramt")], now=make_now(), srs=srs, rng=random.Random(1))
    brief = render_brief(plan, booking="Erinnerung angelegt", closing="Bis heute Abend!")
    assert brief.startswith("Guten Morgen! ☕ Bereit für 5 Minuten? Heute: „Behörden und Termine“.")
    assert "Heute im Kalender: 16:00 Anmeldung Bürgeramt" in brief
    assert "Deine Phrasen für heute:" in brief
    assert "1. " in brief and "Erinnerung angelegt" in brief and "Bis heute Abend!" in brief
    assert "|" not in brief  # no markdown tables — Telegram/TTS-safe
    assert all(not line.lstrip().startswith("#") for line in brief.splitlines())


def test_render_tag_shows_plan_and_source(srs):
    ensure_seeded(srs)
    plan = build_day_plan(events=[event("Anmeldung Bürgeramt")], now=make_now(), srs=srs, rng=random.Random(1))
    tag = render_tag(plan)
    assert "📋 Dein Tag" in tag and "Behörden und Termine" in tag
    assert "aus deinem Kalender" in tag
    assert "/erfolge" in tag
    plan.phrases = plan.phrases[:5]
    fallback_tag = render_tag(
        DayPlan(date="2026-10-02", topic="Smalltalk", phrases=plan.phrases, source="fallback")
    )
    assert "kein Kalender gelesen" in fallback_tag


def test_day_plan_json_roundtrip_and_tolerance():
    plan = DayPlan(date="2026-10-02", topic="T", phrases=["a", "b"], source="calendar", events=[("16:00", "X")])
    assert DayPlan.from_json(json.loads(json.dumps(plan.to_json()))) == plan
    assert DayPlan.from_json(None) is None
    assert DayPlan.from_json({"phrases": "kaputt"}) is None
    assert DayPlan.from_json({"phrases": [1, "ok"]}).phrases == ["1", "ok"]
    assert DayPlan.from_json({"phrases": ["a"], "events": [[3, "x"]]}).events == []


def test_phrase_vocab_hits_word_boundary():
    assert phrase_vocab_hits("ich habe das brot gekauft", "das Brot")
    assert phrase_vocab_hits("ich esse brot", "das Brot")  # bare noun hits too
    assert not phrase_vocab_hits("ich esse ein brotchen", "das Brot")  # Brötchen ≠ Brot
    assert not phrase_vocab_hits("völlig weg", "das Brot")  # no substring inside a word
    assert not phrase_vocab_hits("nichts hier", "das Brot")
