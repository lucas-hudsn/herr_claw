"""`herr-claw daemon` — Telegram loop + break-glass runner (P4; P5 re-scoped).

The ONE scheduler is the OpenClaw cron inside sandbox `my-assistant`
(SPEC §4.1, P5): cron fires run `herr-claw daemon --trigger <job>` in the
sandbox → ONE audited `run_job` bridge call → the job body executes HERE,
host-side (agent/jobs.py), with the same dedup, state and Telegram path
as this loop. What remains for the host loop is the interactive Telegram
long-poll (quiz answers, slash commands) plus manual/break-glass use —
`--once` semantics via `--trigger`, or the full loop below.

Jobs at a glance (times from agent/schedule.yaml, Berlin wall clock):

- 08:00 Morning Nudge — today's calendar suggests the topic, a free 15-min
  slot is booked (Reminder in 'Deutsch' + event in 'Deutsch Lernen' via the
  MCP bridge), and the nudge lands on the phone via Telegram.
- 12:30 Micro-quiz — 3 questions from the weakest vocab, but only if the
  nudge ran today (SPEC §4.1). Answered interactively on Telegram.
- 20:00 Recap — appends the daily note + Deutsch/progress.md to Obsidian
  and marks the day's practice reminders complete.

Job dedup lives in session.json (`jobs_done`: job → ISO date) — cron fires
and a restarted daemon can never fire the same job twice on one day.

Scheduler invariant (AGENTS.md): the OpenClaw cron IS the one scheduling
mechanism; no launchd, no second cron, no second schedule config. This
loop consumes the same schedule only for catch-up/interactive duty.
"""

from __future__ import annotations

import random
import time as _time  # datetime.time shadows the stdlib module below
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .bridge import BridgeClient, BridgeError
from .chat import load_soul
from .commands import ChatContext, Direct, ToLLM, dispatch
from .config import Config, load_config
from .llm import LLMError, Tutor, make_client, parse_correction
from .quiz import ensure_seeded, load_seed, start as start_quiz, weakest_words
from .scheduling import book_topic_session, fetch_today_events, suggest_topic
from .state import SessionState, SrsState
from .telegram import TelegramClient, TelegramError, parse_update
from .tracker import (
    DAILY_NOTE_FMT,
    PROGRESS_FILE,
    PROGRESS_HEADER,
    Tracker,
    safe_append,
)

SCHEDULE_PATH = Path(__file__).resolve().parent / "schedule.yaml"
DEFAULT_JOBS = {"nudge": time(8, 0), "quiz": time(12, 30), "recap": time(20, 0)}
POLL_CAP_S = 50  # long-poll ceiling; jobs are minute-precision, this is plenty
RETRY_BACKOFF_S = 10  # wait after a failed Telegram poll / failed job
DEFAULT_TOPIC = "Smalltalk und Alltag"
TELEGRAM_MAX_TOKENS = 300  # phone replies stay short, like the voice mode

DAEMON_BANNER = (
    "Herr Claw 🗓️ — Daemon (Täglich-Schleife + Telegram)\n"
    "08:00 Nudge · 12:30 Mini-Quiz · 20:00 Recap · Telegram-Bot läuft.\n"
    "Stoppen: Strg-C"
)

REMINDER_COMPLETE_KEY = "5-Min Chat"  # reminders_complete matches by substring


def load_schedule(path: Path = SCHEDULE_PATH, output_fn=print) -> dict[str, time]:
    """agent/schedule.yaml → {job: local time}. Falls back to the built-in
    §4.1 times when the file is missing or unreadable (with a warning)."""
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        jobs = raw.get("jobs") or {}
        parsed: dict[str, time] = {}
        for name, hhmm in jobs.items():
            hour, minute = str(hhmm).split(":")
            parsed[str(name)] = time(int(hour), int(minute))
        if parsed:
            return parsed
        output_fn(f"⚠︎ {path.name}: keine Jobs gefunden — Standardzeiten aus SPEC §4.1.")
    except FileNotFoundError:
        pass
    except Exception as exc:  # malformed yaml etc. — never break the daemon
        output_fn(f"⚠︎ {path.name} unlesbar ({type(exc).__name__}) — Standardzeiten aus SPEC §4.1.")
    return dict(DEFAULT_JOBS)


def next_run(job_time: time, now: datetime) -> datetime:
    """Today at job_time, or tomorrow when it already passed."""
    candidate = datetime.combine(now.date(), job_time)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class Daemon:
    """The scheduler + Telegram loop. All collaborators are injectable so
    tests run without Telegram, Apple or NVIDIA."""

    def __init__(
        self,
        cfg: Config,
        *,
        srs: SrsState | None = None,
        session: SessionState | None = None,
        telegram: TelegramClient | None = None,
        chat_id: str = "",
        bridge: BridgeClient | None = None,
        vault=None,  # herrclaw_bridge.vault.Vault | None
        tutor: Tutor | None = None,
        jobs: dict[str, time] | None = None,
        rng: random.Random | None = None,
        now_fn=None,
        output_fn=print,
    ) -> None:
        self.cfg = cfg
        self.srs = srs or SrsState(cfg.srs_path)
        self.session = session or SessionState.load(cfg.session_path)
        self.telegram = telegram
        self.chat_id = chat_id
        self.bridge = bridge
        self.vault = vault
        self.tutor = tutor
        self.jobs = jobs or load_schedule()
        self.rng = rng or random.Random()
        self._now = now_fn or datetime.now
        self.output = output_fn
        self.tracker = Tracker(vault=vault, srs=self.srs)
        self.ctx = ChatContext(srs=self.srs, session=self.session, tracker=self.tracker, bridge=bridge)
        self.history: list[dict[str, str]] = []

    # -- schedule helpers --------------------------------------------------

    def _today(self) -> str:
        return self._now().date().isoformat()

    def _paused(self) -> bool:
        pause = self.session.pause_until
        return pause is not None and date.today() < pause

    def pending_jobs(self, now: datetime) -> list[str]:
        """Jobs whose time passed today and that have not run today, oldest
        first (catch-up order)."""
        today = now.date().isoformat()
        return [
            name
            for name, job_time in sorted(self.jobs.items(), key=lambda item: item[1])
            if job_time <= now.time() and self.session.jobs_done.get(name) != today
        ]

    def next_wakeup(self, now: datetime) -> datetime:
        """Earliest next fire among jobs that have not run today."""
        today = now.date().isoformat()
        candidates = [
            next_run(job_time, now)
            for name, job_time in sorted(self.jobs.items(), key=lambda item: item[1])
            if self.session.jobs_done.get(name) != today
        ]
        return min(candidates) if candidates else datetime.combine(
            now.date() + timedelta(days=1), min(self.jobs.values())
        )

    def _run_job(self, name: str, now: datetime) -> bool:
        handler = {
            "nudge": self.job_nudge,
            "quiz": self.job_quiz,
            "recap": self.job_recap,
        }.get(name)
        if handler is None:
            self.output(f"⚠︎ Unbekannter Job „{name}“ in schedule.yaml — übersprungen.")
            return True  # unknown jobs never block the loop
        if self._paused():
            self.output(f"⏸ „{name}“ übersprungen — Pause bis {self.session.pause_until}.")
            self.session.jobs_done[name] = now.date().isoformat()
            self.session.save()
            return True  # paused days skip once; tomorrow resumes
        try:
            handler(now)
        except Exception as exc:
            self.output(f"⚠︎ Job „{name}“ fehlgeschlagen ({type(exc).__name__}: {exc}).")
            return False  # not marked done → retried on the next loop pass
        self.session.jobs_done[name] = now.date().isoformat()
        self.session.save()
        return True

    # -- the three §4.1 jobs ------------------------------------------------

    def _send(self, text: str) -> None:
        if self.telegram is None:
            return
        try:
            self.telegram.send_message(self.chat_id, text)
        except TelegramError as exc:
            self.output(f"⚠︎ Telegram-Sendung fehlgeschlagen: {exc}")

    def _fallback_topic(self) -> str | None:
        """Theme of the weakest vocab — ties the SRS to the daily topic.
        None when there is nothing to learn from (never invents a calendar
        connection; the calendar-derived topic comes from suggest_topic)."""
        weakest = weakest_words(self.srs, 1)
        if not weakest:
            return None
        meta = load_seed().get(weakest[0], {})
        return meta.get("theme") or None

    def job_nudge(self, now: datetime) -> None:
        events: list[dict] = []
        calendar_ok = True
        if self.bridge is not None:
            try:
                events = fetch_today_events(self.bridge, now)
            except BridgeError:
                calendar_ok = False  # degrade honestly — the nudge still goes out
        topic = suggest_topic(events, now) or self._fallback_topic() or DEFAULT_TOPIC
        if self.bridge is None:
            booking = "Keine Bridge konfiguriert — heute keine Buchung (siehe HERR_BRIDGE_URL)."
        else:
            # readable calendar → reuse the fetched events; after a fetch
            # failure let the booking re-fetch so its "Keine Kalenderprüfung"
            # honesty reaches the user instead of a slot picked from nothing
            booking = book_topic_session(
                self.bridge, topic, now, events=events if calendar_ok else None
            )
        self._send(f"Guten Morgen! ☕ Bereit für 5 Minuten? Heute: „{topic}“.\n{booking}")

    def job_quiz(self, now: datetime) -> None:
        if self.session.jobs_done.get("nudge") != now.date().isoformat():
            self.output("ℹ︎ 12:30-Quiz übersprungen — der Morgen-Nudge lief heute nicht.")
            return
        if self.ctx.quiz is not None and not self.ctx.quiz.finished:
            return  # a quiz is already running — don't stack another one
        quiz = start_quiz(self.srs, n=3, rng=self.rng, today=now.date())
        self.ctx.quiz = quiz
        self._send(
            "🎒 Mini-Quiz — deine 3 schwersten Vokabeln. Antworte einfach hier "
            "(„ende“ beendet).\n\n" + quiz.intro()
        )

    def job_recap(self, now: datetime) -> None:
        avg = (
            sum(e.level for e in self.srs.vocab.values()) / len(self.srs.vocab)
            if self.srs.vocab
            else 0.0
        )
        topic = self.session.current_topic or ""
        completed = ""
        try:
            if self.bridge is not None:
                completed = self.bridge.call_tool("reminders_complete", {"title": REMINDER_COMPLETE_KEY})
        except BridgeError as exc:
            completed = f"Erinnerungen nicht abgehakt: {exc}"
        daily = (
            f"\n## Tages-Recap — {now.strftime('%H:%M')}\n"
            f"- Streak: {self.srs.streak.current} Tag(e) · "
            f"Gesamt: {self.srs.totals.messages} Nachrichten, "
            f"{self.srs.totals.corrections} Korrekturen\n"
            f"- Vokabeln im SRS: {len(self.srs.vocab)} (Ø Level {avg:.1f})\n"
        )
        if topic:
            daily += f"- Thema: {topic}\n"
        if completed:
            daily += f"- Erinnerungen: {completed}\n"
        if self.vault is not None:
            daily_rel = now.strftime(DAILY_NOTE_FMT)
            safe_append(self.vault, daily_rel, daily)
            safe_append(
                self.vault,
                PROGRESS_FILE,
                (
                    f"- {now.date().isoformat()} — Tages-Recap {now.strftime('%H:%M')}: "
                    f"Streak {self.srs.streak.current} Tag(e) · "
                    f"{len(self.srs.vocab)} Vokabeln (Ø Level {avg:.1f})"
                ),
                header=PROGRESS_HEADER,
            )
        else:
            self.output("⚠︎ HERR_VAULT nicht gesetzt — Recap nur in state/.")
        self._send(
            f"📚 Tages-Recap gespeichert. Streak: {self.srs.streak.current} Tag(e). "
            "Gute Nacht! 🌙"
        )

    # -- Telegram turns -----------------------------------------------------

    def _tutor_exchange(self, user_message: str, system_note: str | None = None) -> str | None:
        self.history.append({"role": "user", "content": user_message})
        try:
            reply = self.tutor.reply(self.history[-12:], system_note=system_note)
        except LLMError as exc:
            self.history.pop()
            return f"⚠︎ {exc}"
        self.history.append({"role": "assistant", "content": reply})
        self.tracker.note_exchange()
        self.srs.add_message()
        self.srs.touch_day()
        correction = parse_correction(reply)
        if correction:
            fix, example = correction
            self.tracker.log_correction(example=example, fix=fix)
        return reply

    def respond(self, text: str) -> str | None:
        """One incoming text → reply text (None → nothing to send)."""
        outcome = dispatch(text, self.ctx)
        if isinstance(outcome, Direct):
            return outcome.text
        if isinstance(outcome, ToLLM):
            return self._tutor_exchange(outcome.user_message, system_note=outcome.system_note)
        return self._tutor_exchange(text)

    def handle_update(self, update: dict) -> None:
        incoming = parse_update(update)
        if incoming is None:
            return
        if self.chat_id and incoming.chat_id != self.chat_id:
            self.output(f"⚠︎ Nachricht von fremder Chat-ID {incoming.chat_id} ignoriert.")
            return
        reply = self.respond(incoming.text)
        if reply:
            self._send(reply)

    # -- main loop ------------------------------------------------------------

    def catch_up(self) -> None:
        """Run jobs missed earlier today, oldest first (an evening start
        still produces the day's artifacts)."""
        for name in self.pending_jobs(self._now()):
            self.output(f"⏰ Nachholprogramm „{name}“ …")
            self._run_job(name, self._now())

    def sync_updates(self) -> int:
        """Skip the update backlog so a restart never re-fires old commands;
        returns the offset to poll from."""
        try:
            latest = self.telegram.get_updates(-1, timeout_s=0) if self.telegram else []
        except TelegramError:
            return 0
        return (latest[-1]["update_id"] + 1) if latest else 0

    def poll(self, offset: int, timeout_s: int) -> int:
        """One Telegram long poll; returns the next offset."""
        try:
            updates = self.telegram.get_updates(offset, timeout_s=timeout_s)
        except TelegramError as exc:
            self.output(f"⚠︎ {exc} — neuer Versuch in {RETRY_BACKOFF_S} s.")
            self._sleep(RETRY_BACKOFF_S)
            return offset
        for update in updates:
            self.handle_update(update)
        return (updates[-1]["update_id"] + 1) if updates else offset

    def _sleep(self, seconds: float) -> None:
        _time.sleep(seconds)

    def run(self) -> int:
        self.output(DAEMON_BANNER)
        try:
            self.catch_up()
            offset = self.sync_updates()
            while True:
                now = self._now()
                due = self.pending_jobs(now)
                if due:
                    for name in due:
                        if not self._run_job(name, now):
                            self._sleep(RETRY_BACKOFF_S)  # avoid a hot retry loop
                    continue
                wakeup = self.next_wakeup(now)
                poll_s = max(1, min(POLL_CAP_S, int((wakeup - now).total_seconds())))
                offset = self.poll(offset, poll_s)
        except KeyboardInterrupt:  # Strg-C — graceful stop, state saved
            pass
        self.save_state()
        return 0

    def save_state(self) -> None:
        self.srs.save()
        self.session.save()
        self.output("👋 Daemon gestoppt — Zustand gespeichert. Tschüss!")


def run_daemon(
    cfg: Config | None = None,
    *,
    telegram: TelegramClient | None = None,
    chat_id: str | None = None,
    bridge: BridgeClient | None = None,
    vault=None,
    tutor: Tutor | None = None,
    output_fn=print,
) -> int:
    """CLI entry for `herr-claw daemon`. Requires the Telegram token + chat
    id — without them there is no phone to nudge and the daemon is pointless."""
    cfg = cfg or load_config()
    if telegram is None:
        from .telegram import client_from_env

        try:
            telegram = client_from_env()
        except TelegramError as exc:
            output_fn(f"⚠︎ {exc}")
            return 1
    chat_id = chat_id if chat_id is not None else cfg.telegram_chat_id
    if not chat_id:
        output_fn("⚠︎ HERR_TELEGRAM_CHAT_ID ist nicht gesetzt — der Bot antwortet sonst niemandem.")
        return 1
    srs = SrsState(cfg.srs_path)
    ensure_seeded(srs)
    session = SessionState.load(cfg.session_path)
    if bridge is None and cfg.bridge_url:
        bridge = BridgeClient(cfg.bridge_url)
    if tutor is None:
        try:
            tutor = Tutor(
                make_client(cfg.base_url),
                model=cfg.model,
                system_prompt=load_soul(),
                max_tokens=TELEGRAM_MAX_TOKENS,
            )
        except LLMError as exc:
            output_fn(f"⚠︎ {exc}")
            return 1
    daemon = Daemon(
        cfg,
        srs=srs,
        session=session,
        telegram=telegram,
        chat_id=chat_id,
        bridge=bridge,
        vault=vault,
        tutor=tutor,
        output_fn=output_fn,
    )
    return daemon.run()
