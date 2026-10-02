"""agent/memory.py — the state/memory.md journal (v0.5, SPEC §4.6).

Atomic save, self-healing load, lenient parsing, and the tailoring-only
rule: memory shapes phrasing/topics/examples, never quiz content.
"""

import json

from agent.memory import (
    LANDING_HIT,
    LANDING_MISS,
    MemoryState,
)


def test_missing_file_loads_empty_defaults(tmp_path):
    memory = MemoryState.load(tmp_path / "memory.md")
    assert memory.profil == [] and memory.interessen == []
    assert memory.erfolge == [] and memory.notizen == []


def test_save_then_load_roundtrip(tmp_path):
    memory = MemoryState.load(tmp_path / "memory.md")
    memory.profil.append("Arbeit: Softwareentwickler")
    memory.record_interest("Bäckerei", landed=True)
    memory.record_success("Ich hätte gern zwei Brötchen", context="Beim Bäcker", today="2026-10-02")
    memory.add_note("mag Beispiele mit dem Namen seiner Schwester")
    memory.save()

    reloaded = MemoryState.load(tmp_path / "memory.md")
    assert reloaded.profil == ["Arbeit: Softwareentwickler"]
    assert [i.topic for i in reloaded.interessen] == ["Bäckerei"]
    assert reloaded.interessen[0].landing == LANDING_HIT
    assert len(reloaded.erfolge) == 1
    assert reloaded.erfolge[0].phrase == "Ich hätte gern zwei Brötchen"
    assert reloaded.erfolge[0].context == "Beim Bäcker"
    assert reloaded.erfolge[0].count == 1
    assert reloaded.notizen == ["mag Beispiele mit dem Namen seiner Schwester"]


def test_save_is_atomic_no_tmp_leftover(tmp_path):
    memory = MemoryState.load(tmp_path / "memory.md")
    memory.save()
    assert (tmp_path / "memory.md").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_record_success_dedups_and_bumps_count(tmp_path):
    memory = MemoryState.load(tmp_path / "memory.md")
    assert memory.record_success("Wie geht es dir?", today="2026-10-02") is True
    assert memory.record_success("wie geht es dir?", today="2026-10-05") is False  # case-lenient
    assert len(memory.erfolge) == 1
    assert memory.erfolge[0].count == 2
    assert memory.erfolge[0].date == "2026-10-05"  # last reported use wins


def test_record_interest_updates_landing(tmp_path):
    memory = MemoryState.load(tmp_path / "memory.md")
    memory.record_interest("Fußball", landed=True)
    memory.record_interest("Fußball", landed=False)  # changed its mind
    assert len(memory.interessen) == 1
    assert memory.interessen[0].landing == LANDING_MISS
    assert memory.interessen[0].count == 2


def test_corrupt_file_backed_up_and_reset(tmp_path):
    path = tmp_path / "memory.md"
    path.write_bytes(b"\xff\xfe not utf-8 \xff")
    memory = MemoryState.load(path)  # must not raise
    assert memory.erfolge == []
    backups = list(tmp_path.glob("memory.md.corrupt-*"))
    assert backups, "original quarantined beside itself, never silently dropped"
    memory.save()  # fresh journal starts cleanly


def test_unparseable_lines_survive_a_save(tmp_path):
    path = tmp_path / "memory.md"
    path.write_text(
        "# Herr Claw — Gedächtnis\n\n## Erfolgreiche Phrasen\n"
        "- „guter Satz“ · Büro · 2026-10-01 · 1×\n- Dieser Satz ist kein Format\n",
        encoding="utf-8",
    )
    memory = MemoryState.load(path)
    memory.record_success("Neuer Satz", today="2026-10-02")
    memory.save()

    text = path.read_text(encoding="utf-8")
    assert "Dieser Satz ist kein Format" in text  # raw lines never dropped
    reloaded = MemoryState.load(path)
    assert {s.phrase for s in reloaded.erfolge} == {"guter Satz", "Neuer Satz"}


def test_prompt_block_cap_and_content(tmp_path):
    memory = MemoryState.load(tmp_path / "memory.md")
    assert memory.prompt_block() == ""  # empty journal → no prompt noise
    memory.profil.append("Arbeit: Softwareentwickler in Berlin")
    memory.record_interest("Bäckerei", landed=True)
    memory.record_interest("Fußball", landed=False)
    memory.record_success("Ein Satz", today="2026-10-01")
    for i in range(7):
        memory.record_success(f"Satz Nummer {i}", today="2026-10-02")
    block = memory.prompt_block(max_phrases=3)
    assert "Softwareentwickler" in block
    assert "Bäckerei" in block and "Fußball" in block  # liked AND skipped shown
    assert "Satz Nummer 6" in block and "Satz Nummer 0" not in block  # top by count/date
    assert len(block) <= 700


def test_memory_is_never_a_quiz_source(srs, tmp_path):
    """Anti-hallucination guard stays intact: quiz content comes from the
    seed + srs.json only — a memory full of exotic words changes nothing."""
    from agent import quiz as quiz_mod
    from agent.quiz import ensure_seeded

    memory = MemoryState.load(tmp_path / "memory.md")
    memory.record_success("Ich möchte einen Drehstablerwerksbesichtigertermin", today="2026-10-02")
    memory.record_interest("Drehstablerwerk", landed=True)
    ensure_seeded(srs)
    questions = quiz_mod.start(srs, n=10)
    assert all(q.key in srs.vocab for q in questions.questions)
    assert all("Drehstablerwerk" not in q.prompt for q in questions.questions)


def test_session_json_still_loads_without_day_plan(config):
    """Backward compatibility: a pre-v0.5 session.json (no day_plan key)
    keeps loading — the ONE state path never breaks on upgrade."""
    config.session_path.parent.mkdir(parents=True, exist_ok=True)
    config.session_path.write_text(
        json.dumps({"version": 1, "current_topic": "Bäckerei"}), encoding="utf-8"
    )
    from agent.state import SessionState

    session = SessionState.load(config.session_path)
    assert session.current_topic == "Bäckerei"
    assert session.day_plan == {}
