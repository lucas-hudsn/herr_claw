"""In-sandbox job execution — the §4.1 job bodies (P6).

The ONE scheduler is the OpenClaw cron inside sandbox `my-assistant`
(SPEC §4.1). A cron fire runs `herr-claw trigger <job>` inside the
sandbox, and since P6 the JOB BODY executes THERE too — the trigger is no
longer a relay to the host:

- state lives with the agent (HERR_STATE_DIR — /sandbox/herrclaw/state);
- Apple/vault access crosses the bridge (BridgeClient → the audited MCP
  tools; vault writes through BridgeVault — the vault never leaves the
  host);
- Telegram sends go straight out through the allowlisted api.telegram.org
  egress (the gateway resolves the token placeholder at egress);
- dedup stays in session.json (`jobs_done`), so repeated cron fires and
  manual triggers can never double-fire a job;
- every trigger brings the day current: earlier jobs whose cron fire
  failed run first (catch-up), so one missed fire no longer loses the
  day — the next fire heals it.

There is no second scheduling or receiving mechanism: the OpenClaw cron
is the ONE scheduler, and the Telegram poller is the ONE conversation
receiver (agent/poller.py). All collaborators are injectable so tests
never touch Telegram, Apple or the network.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time
from pathlib import Path

from .bridge import BridgeClient, BridgeVault
from .commands import ChatContext
from .config import Config, load_config
from .memory import MemoryState
from .phrases import (
    build_day_plan,
    fallback_topic,
    render_brief,
    render_plan_lines,
)
from .quiz import ensure_seeded, start as start_quiz
from .scheduling import book_topic_session, fetch_today_events, suggest_topic
from .state import SessionState, SrsState
from .telegram import TelegramClient, TelegramError, client_from_env
from .tracker import (
    DAILY_NOTE_FMT,
    PROGRESS_FILE,
    PROGRESS_HEADER,
    Tracker,
    safe_append,
)
from herrclaw_bridge.errors import BridgeError

SCHEDULE_PATH = Path(__file__).resolve().parent / "schedule.yaml"
DEFAULT_JOBS = {"nudge": time(8, 0), "quiz": time(12, 30), "recap": time(20, 0)}

REMINDER_COMPLETE_KEY = "5-Min Chat"  # reminders_complete matches by substring


def load_schedule(path: Path = SCHEDULE_PATH, output_fn=print) -> dict[str, time]:
    """agent/schedule.yaml → {job: local time}. Falls back to the built-in
    §4.1 times when the file is missing or unreadable (with a warning)."""
    notify = output_fn or (lambda line: None)
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
        notify(f"⚠︎ {path.name}: keine Jobs gefunden — Standardzeiten aus SPEC §4.1.")
    except FileNotFoundError:
        pass
    except Exception as exc:  # malformed yaml etc. — never break the trigger path
        notify(f"⚠︎ {path.name} unlesbar ({type(exc).__name__}) — Standardzeiten aus SPEC §4.1.")
    return dict(DEFAULT_JOBS)


class JobRunner:
    """Executes the §4.1 jobs in the sandbox, exactly once per job per day.

    One instance lives for exactly one trigger fire (see run_job_once):
    it catches the day up, runs the named job, and dies. There is no
    loop here — the loop is the OpenClaw cron in the sandbox."""

    def __init__(
        self,
        cfg: Config,
        *,
        srs: SrsState | None = None,
        session: SessionState | None = None,
        telegram: TelegramClient | None = None,
        chat_id: str = "",
        bridge: BridgeClient | None = None,
        vault=None,  # Vault or BridgeVault; None → no vault writes
        jobs: dict[str, time] | None = None,
        memory: MemoryState | None = None,
        rng: random.Random | None = None,
        now_fn=None,
        output_fn=print,
    ) -> None:
        self.cfg = cfg
        self.srs = srs or SrsState(cfg.srs_path)
        self.session = session or SessionState.load(cfg.session_path)
        self.memory = memory or MemoryState.load(cfg.memory_path)
        self.telegram = telegram
        self.chat_id = chat_id
        self.bridge = bridge
        self.vault = vault
        self.jobs = jobs or load_schedule(output_fn=output_fn)
        self.rng = rng or random.Random()
        self._now = now_fn or datetime.now
        self.output = output_fn
        self.tracker = Tracker(vault=vault, srs=self.srs)
        self.ctx = ChatContext(
            srs=self.srs,
            session=self.session,
            tracker=self.tracker,
            bridge=self.bridge,
            memory=self.memory,
            rng=self.rng,
        )

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

    def run(self, name: str, now: datetime) -> bool:
        """Run one named job; returns False when it failed (the caller
        decides what that means — cron fires retry on the NEXT fire)."""
        handler = {
            "nudge": self.job_nudge,
            "quiz": self.job_quiz,
            "recap": self.job_recap,
        }.get(name)
        if handler is None:
            self.output(f"⚠︎ Unbekannter Job „{name}“ in schedule.yaml — übersprungen.")
            return True  # unknown jobs never block the day
        if self._paused():
            self.output(f"⏸ „{name}“ übersprungen — Pause bis {self.session.pause_until}.")
            self.session.jobs_done[name] = now.date().isoformat()
            self.session.save()
            return True  # paused days skip once; tomorrow resumes
        try:
            handler(now)
        except Exception as exc:
            self.output(f"⚠︎ Job „{name}“ fehlgeschlagen ({type(exc).__name__}: {exc}).")
            return False  # not marked done → retried on the next fire
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

    def job_nudge(self, now: datetime) -> None:
        events: list[dict] = []
        calendar_ok = True
        if self.bridge is not None:
            try:
                events = fetch_today_events(self.bridge, now)
            except BridgeError:
                calendar_ok = False  # degrade honestly — the nudge still goes out
        topic = suggest_topic(events, now) or fallback_topic(self.srs)
        plan = build_day_plan(
            events=events,
            now=now,
            srs=self.srs,
            memory=self.memory,
            rng=self.rng,
            topic=topic,
        )
        self.session.day_plan = plan.to_json()  # /tag re-shows this all day
        self.session.save()
        if self.bridge is None:
            booking = "Keine Bridge konfiguriert — heute keine Buchung (siehe HERR_BRIDGE_URL)."
        else:
            # readable calendar → reuse the fetched events; after a fetch
            # failure let the booking re-fetch so its "Keine Kalenderprüfung"
            # honesty reaches the user instead of a slot picked from nothing
            booking = book_topic_session(
                self.bridge, topic, now, events=events if calendar_ok else None
            )
        self._send(
            render_brief(
                plan,
                booking=booking,
                closing="Erzähl mir heute Abend, welche du benutzt hast! (/erfolge <Satz>)",
            )
        )
        if self.vault is not None:
            brief_note = (
                f"\n## Morgen-Brief — {now.strftime('%H:%M')}\n"
                f"- Thema: {plan.topic}\n\n{render_plan_lines(plan)}\n"
            )
            safe_append(self.vault, now.strftime(DAILY_NOTE_FMT), brief_note)

    def job_quiz(self, now: datetime) -> None:
        if self.session.jobs_done.get("nudge") != now.date().isoformat():
            self.output("ℹ︎ 12:30-Quiz übersprungen — der Morgen-Nudge lief heute nicht.")
            return
        if self.ctx.quiz is not None and not self.ctx.quiz.finished:
            return  # a quiz is already running — don't stack another one
        quiz = start_quiz(self.srs, n=3, rng=self.rng, today=now.date())
        self.ctx.quiz = quiz
        # persist: the Telegram poller (or the next `turn`) grades the answers
        self.session.active_quiz = quiz.to_json()
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
            self.output("⚠︎ Kein Vault-Zugang (Bridge aus?) — Recap nur in state/.")
        plan = self.ctx.today_plan()
        erfolgs_check = (
            f"Abend-Check: Welche der {len(plan.phrases)} heutigen Phrasen hast du benutzt? "
            if plan
            else "Abend-Check: Welche Phrasen hast du heute benutzt? "
        )
        self._send(
            f"📚 Tages-Recap gespeichert. Streak: {self.srs.streak.current} Tag(e). "
            f"{erfolgs_check}Schreib sie einfach — oder kurz: /erfolge <Satz>. Gute Nacht! 🌙"
        )


def run_job_once(
    name: str,
    *,
    cfg: Config | None = None,
    bridge: BridgeClient | None = None,
    telegram: TelegramClient | None = None,
    vault=None,
    now: datetime | None = None,
    output_fn=None,
) -> str:
    """Run one §4.1 job in the sandbox, exactly once per day (session.json
    dedup). Every trigger first brings the day current: earlier jobs whose
    cron fire failed (bridge blip, sandbox restart) run before the named
    one — a 12:30 quiz fire heals a lost 08:00 nudge instead of skipping
    itself. Dedup makes this idempotent; a job can never run twice a day.

    Returns a German result text for the cron log; raises BridgeError for
    unknown job names (trigger_job never retries those)."""
    cfg = cfg or load_config()
    if bridge is None:
        bridge = BridgeClient(cfg.bridge_url) if cfg.bridge_url else None
    if vault is None and bridge is not None:
        vault = BridgeVault(bridge)
    if telegram is None:
        try:
            telegram = client_from_env()
        except TelegramError:
            telegram = None  # no token → jobs still run, sends are skipped

    jobs = load_schedule(output_fn=output_fn)
    if name not in jobs:
        raise BridgeError(
            f"Unbekannter Job „{name}“ — erlaubt sind: {', '.join(sorted(jobs))}."
        )

    lines: list[str] = []

    def collect(line: str) -> None:
        lines.append(line)
        if output_fn is not None:
            output_fn(line)

    srs = SrsState(cfg.srs_path)
    ensure_seeded(srs)
    session = SessionState.load(cfg.session_path)
    now = now or datetime.now()
    runner = JobRunner(
        cfg,
        srs=srs,
        session=session,
        telegram=telegram,
        chat_id=cfg.telegram_chat_id,
        bridge=bridge,
        vault=vault,
        jobs=jobs,
        output_fn=collect,
    )
    # catch-up: earlier jobs whose time passed and that have not run today,
    # oldest first (pending_jobs is dedup-aware, so this is idempotent)
    for missed in [j for j in runner.pending_jobs(now) if j != name]:
        ok = runner.run(missed, now)
        lines.append(f"⏰ Nachgeholt: „{missed}“ — " + ("erledigt" if ok else "fehlgeschlagen"))
    if session.jobs_done.get(name) == now.date().isoformat():
        return "\n".join(
            [f"Job „{name}“ lief heute schon — übersprungen (Dedup über session.json).", *lines]
        )
    ok = runner.run(name, now)
    status = "erledigt" if ok else "fehlgeschlagen (wird beim nächsten Feuer erneut versucht)"
    header = f"Job „{name}“ {status}."
    return "\n".join([header, *lines]) if lines else header
