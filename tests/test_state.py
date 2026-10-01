"""State persistence, streak logic, mistake dedup, SM-2-lite reviews."""

from datetime import date, timedelta

from agent.state import INTERVALS_DAYS, SessionState, SrsState, VocabEntry


class TestPersistence:
    def test_fresh_state_defaults(self, config):
        srs = SrsState(config.srs_path)
        assert srs.vocab == {} and srs.mistakes == []
        assert srs.streak.current == 0 and srs.totals.messages == 0

    def test_roundtrip(self, config):
        srs = SrsState(config.srs_path)
        srs.vocab["das Brot"] = VocabEntry(en="bread", level=3, next_due=date(2026, 10, 8), article="das", plural="die Brote")
        srs.add_mistake("ich bin ging", "ich ging", pattern="Präteritum")
        srs.add_mistake("ich bin ging", "ich ging")  # repeat → count only
        srs.touch_day(date(2026, 10, 1))
        srs.add_message()
        srs.totals.sessions = 2
        srs.save()

        loaded = SrsState(config.srs_path)
        assert loaded.vocab["das Brot"].en == "bread"
        assert loaded.vocab["das Brot"].next_due == date(2026, 10, 8)
        assert loaded.vocab["das Brot"].plural == "die Brote"
        assert len(loaded.mistakes) == 1 and loaded.mistakes[0].count == 2
        assert loaded.streak.current == 1 and loaded.streak.last_day == date(2026, 10, 1)
        assert loaded.totals.messages == 1 and loaded.totals.corrections == 2
        assert loaded.totals.sessions == 2

    def test_corrupt_file_backed_up_and_reset(self, config):
        config.srs_path.parent.mkdir(parents=True)
        config.srs_path.write_text("{not json!!", encoding="utf-8")
        srs = SrsState(config.srs_path)
        assert srs.vocab == {}
        backups = list(config.srs_path.parent.glob("srs.json.corrupt-*"))
        assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{not json!!"

    def test_session_roundtrip(self, config):
        session = SessionState.load(config.session_path)
        session.current_topic = "Bäckerei"
        session.pause_until = date(2026, 10, 5)
        session.last_session_turns = 7
        session.save()
        loaded = SessionState.load(config.session_path)
        assert loaded.current_topic == "Bäckerei"
        assert loaded.pause_until == date(2026, 10, 5)
        assert loaded.last_session_turns == 7


class TestStreak:
    def test_new_day_then_same_day_once(self, srs):
        d1 = date(2026, 10, 1)
        assert srs.touch_day(d1) is True
        assert srs.streak.current == 1
        assert srs.touch_day(d1) is False
        assert srs.streak.current == 1

    def test_consecutive_days_accumulate(self, srs):
        d1 = date(2026, 10, 1)
        srs.touch_day(d1)
        srs.touch_day(d1 + timedelta(days=1))
        assert srs.streak.current == 2
        assert srs.streak.longest == 2

    def test_gap_resets_but_keeps_record(self, srs):
        d1 = date(2026, 10, 1)
        srs.touch_day(d1)
        srs.touch_day(d1 + timedelta(days=1))
        srs.touch_day(d1 + timedelta(days=5))
        assert srs.streak.current == 1
        assert srs.streak.longest == 2


class TestMistakes:
    def test_new_pair_then_dedup(self, srs):
        assert srs.add_mistake("ich bin ging", "ich ging") is True
        assert srs.add_mistake("ich bin ging", "ich ging") is False
        assert srs.add_mistake("ich bin ging", "ich gehe") is True  # different fix → new entry
        assert [m.count for m in srs.mistakes] == [2, 1]
        assert srs.totals.corrections == 3

    def test_last_seen_updated_on_repeat(self, srs):
        srs.add_mistake("x", "y")
        first = srs.mistakes[0].last_seen
        srs.add_mistake("x", "y")
        assert srs.mistakes[0].last_seen >= first


class TestSrs:
    def seed(self, srs):
        srs.vocab["das Brot"] = VocabEntry(en="bread", level=3)

    def test_hit_levels_up_and_sets_due_by_new_level(self, srs):
        self.seed(srs)
        today = date(2026, 10, 1)
        entry = srs.apply_review("das Brot", correct=True, today=today)
        assert entry.level == 4
        assert entry.next_due == today + timedelta(days=INTERVALS_DAYS[4])

    def test_miss_drops_level_and_resets_due(self, srs):
        self.seed(srs)
        today = date(2026, 10, 1)
        entry = srs.apply_review("das Brot", correct=False, today=today)
        assert entry.level == 2 and entry.misses == 1
        assert entry.next_due == today + timedelta(days=INTERVALS_DAYS[2])

    def test_hit_at_max_stays_and_miss_at_floor_stays(self, srs):
        srs.vocab["das Brot"] = VocabEntry(en="bread", level=5)
        srs.vocab["der Kater"] = VocabEntry(en="hangover", level=1)
        today = date(2026, 10, 1)
        assert srs.apply_review("das Brot", True, today).level == 5
        assert srs.apply_review("der Kater", False, today).level == 1
        assert srs.vocab["der Kater"].misses == 1

    def test_due_filter(self, srs):
        today = date(2026, 10, 1)
        srs.vocab["das Brot"] = VocabEntry(en="bread", level=1, next_due=today)
        srs.vocab["die Milch"] = VocabEntry(en="milk", level=1, next_due=today + timedelta(days=3))
        srs.vocab["der Apfel"] = VocabEntry(en="apple", level=1, next_due=None)
        assert set(srs.due(today)) == {"das Brot", "der Apfel"}
