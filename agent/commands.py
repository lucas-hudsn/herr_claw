"""The stable German slash commands (AGENTS.md — do not rename, no aliases).

P1 implemented the state-only commands (/fortschritt, /fehler, /pause,
/erkläre, /üben topic steering); P4 replaced the /quiz stub with the real
SRS quiz (agent/quiz.py) and answers /sprechen truthfully (P3 shipped it).
v0.5 adds /tag (today's tailored phrase plan) and /erfolge (report a phrase
you used successfully) — SPEC §4.2. All frontends (chat, sprechen,
Telegram) go through dispatch(), so every command behaves identically
everywhere. While a quiz is active, plain text is graded as an answer and
„ende“ quits.

note_user_exchange() is the ONE bookkeeping path every frontend calls after
a tutor exchange: correction extraction, and success recording — a reported
day-plan phrase lands in state/memory.md, reinforces the matching SRS
entries, and is appended to Deutsch/progress.md (SPEC §4.1, 20:00 job).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import quiz as quiz_mod
from .bridge import BridgeClient, BridgeError
from .llm import parse_correction
from .memory import MemoryState
from .phrases import DayPlan, build_day_plan, phrase_vocab_hits, render_tag
from .scheduling import book_topic_session, fetch_today_events
from .state import SessionState, SrsState
from .tracker import PROGRESS_FILE, PROGRESS_HEADER, Tracker, safe_append

COMMANDS = (
    "/üben",
    "/quiz",
    "/fehler",
    "/erfolge",
    "/tag",
    "/fortschritt",
    "/erkläre",
    "/pause",
    "/sprechen",
)

MAX_PAUSED_DAYS = 365


@dataclass
class ChatContext:
    srs: SrsState
    session: SessionState
    tracker: Tracker
    bridge: BridgeClient | None = None  # None → /üben works, just doesn't book
    quiz: quiz_mod.QuizSession | None = None  # active quiz (P4); None when idle
    memory: MemoryState | None = None  # state/memory.md journal (v0.5)
    rng: random.Random | None = None

    def today_plan(self) -> DayPlan | None:
        """The Morgen-Brief when it was built today, else None."""
        plan = DayPlan.from_json(self.session.day_plan)
        if plan and plan.date == date.today().isoformat() and plan.phrases:
            return plan
        return None

    def build_plan(self, topic: str | None = None) -> DayPlan:
        """(Re)build today's plan with the same rules as the Morgen-Brief:
        bridge events when readable, else honest fallback (SPEC §4.6)."""
        events: list[dict] = []
        if self.bridge is not None:
            try:
                events = fetch_today_events(self.bridge)
            except BridgeError:
                events = []
        plan = build_day_plan(
            events=events,
            now=datetime.now(),
            srs=self.srs,
            memory=self.memory,
            rng=self.rng,
            topic=topic,
        )
        self.session.day_plan = plan.to_json()
        self.session.save()
        return plan

    def record_success(self, phrase: str, context: str = "") -> bool:
        """One reported success → memory journal + SRS reinforcement +
        Deutsch/progress.md (append-only, same allowlist). Returns True when
        the phrase is new in the journal."""
        if not context:
            plan = self.today_plan()
            context = self.session.current_topic or (plan.topic if plan else "")
        recorded = False
        if self.memory is not None:
            recorded = self.memory.record_success(phrase, context=context)
            if context:
                self.memory.record_interest(context, landed=True)
            self.memory.save()
        # Reinforce every SRS word the phrase contains (word-boundary match,
        # umlaut/punctuation-lenient like the quiz checker).
        norm = quiz_mod.normalize(phrase)
        for key in self.srs.vocab:
            if phrase_vocab_hits(norm, key):
                self.srs.apply_review(key, correct=True)
        if self.srs.vocab:
            self.srs.save()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        safe_append(
            self.tracker.vault,
            PROGRESS_FILE,
            f"- {stamp} — Erfolg: „{phrase.strip()}“" + (f" · Thema: {self.session.current_topic}" if self.session.current_topic else ""),
            header=PROGRESS_HEADER,
        )
        return recorded


@dataclass
class Direct:
    """Reply without the LLM (command output)."""

    text: str


@dataclass
class ToLLM:
    """Send a message through the tutor, with optional extra system note.

    success_phrases carries phrases the user just reported via /erfolge —
    the frontend records them after the exchange (the gentle correction may
    adjust the German first, but the success counts either way)."""

    user_message: str
    system_note: str | None = None
    success_phrases: tuple[str, ...] = field(default_factory=tuple)


def progress_text(srs: SrsState, session: SessionState) -> str:
    lines = ["📊 Dein Fortschritt:"]
    lines.append(
        f"Streak: {srs.streak.current} Tag(e) · Rekord: {srs.streak.longest} Tag(e)"
    )
    lines.append(
        f"Gesamt: {srs.totals.messages} Nachrichten, {srs.totals.corrections} Korrekturen, "
        f"{srs.totals.sessions} Sitzungen"
    )
    if srs.vocab:
        avg = sum(e.level for e in srs.vocab.values()) / len(srs.vocab)
        lines.append(f"Vokabeln im SRS: {len(srs.vocab)} (Ø Level {avg:.1f})")
    else:
        lines.append("Vokabeln: noch keine — starte /quiz, dann legen wir los!")
    if srs.mistakes:
        last = srs.mistakes[-1]
        lines.append(f"Letzte Korrektur: „{last.example}“ → „{last.fix}“")
    if session.current_topic:
        lines.append(f"Aktuelles Thema: {session.current_topic}")
    if session.pause_until:
        lines.append(f"⏸ Pause bis {session.pause_until.isoformat()}")
    return "\n".join(lines)


def fehler_text(srs: SrsState, limit: int = 5) -> str:
    if not srs.mistakes:
        return "Sehr gut — ich habe noch keine Fehler von dir notiert! 🎉"
    lines = [f"📝 Deine letzten {min(limit, len(srs.mistakes))} Fehler:"]
    for m in srs.mistakes[-limit:][::-1]:
        times = f" ({m.count}×)" if m.count > 1 else ""
        lines.append(f"„{m.example}“ → „{m.fix}“{times}")
    return "\n".join(lines)


def erfolge_text(ctx: ChatContext) -> str:
    """/erfolge without an argument — the journal + today's prompt."""
    memory = ctx.memory
    lines = ["🌟 Deine Erfolge:"]
    if memory is None or not memory.erfolge:
        lines.append(
            "Noch keine aufgezeichnet. Sag mir, welche Phrase du heute benutzt hast: /erfolge <Satz>"
        )
        return "\n".join(lines)
    for success in memory.erfolge[-5:][::-1]:
        times = f" ({success.count}×)" if success.count > 1 else ""
        context = f" · {success.context}" if success.context else ""
        lines.append(f"„{success.phrase}“{times}{context}")
    plan = ctx.today_plan()
    if plan:
        lines.append("")
        lines.append("Welche der heutigen Phrasen hast du benutzt? /erfolge <Satz>")
    return "\n".join(lines)


def reported_phrases(text: str, ctx: ChatContext) -> list[str]:
    """Day-plan phrases the user's message mentions (lenient match, like the
    quiz checker) — the conversational path of the evening Erfolgs-Check."""
    plan = ctx.today_plan()
    if plan is None:
        return []
    norm_text = quiz_mod.normalize(text)
    hits = []
    for phrase in plan.phrases:
        norm_phrase = quiz_mod.normalize(phrase)
        if len(norm_phrase) >= 8 and norm_phrase in norm_text:
            hits.append(phrase)
    return hits


def note_user_exchange(
    ctx: ChatContext,
    user_text: str,
    reply: str,
    extra_successes: tuple[str, ...] = (),
) -> None:
    """ONE post-exchange bookkeeping path for all frontends: counts the turn,
    logs corrections from the SOUL template, records reported successes."""
    ctx.tracker.note_exchange()
    ctx.srs.add_message()
    ctx.srs.touch_day()
    correction = parse_correction(reply)
    if correction:
        fix, example = correction
        ctx.tracker.log_correction(example=example, fix=fix)
    for phrase in dict.fromkeys([*extra_successes, *reported_phrases(user_text, ctx)]):
        ctx.record_success(phrase)


def dispatch(raw: str, ctx: ChatContext) -> Direct | ToLLM | None:
    """Return Direct (print), ToLLM (route through tutor) or None (normal chat).

    While a quiz is active, plain text is graded as an answer and „ende“
    (q/quit/stop/fertig/stopp) quits the quiz; slash commands still work."""
    if not raw.startswith("/"):
        if ctx.quiz is not None and not ctx.quiz.finished:
            if quiz_mod.normalize(raw) in quiz_mod.QUIT_WORDS:
                return Direct(ctx.quiz.cancel())
            outcome = Direct(ctx.quiz.answer(raw))
            ctx.srs.add_message()
            ctx.srs.touch_day()
            if ctx.quiz.finished:
                ctx.quiz = None
            return outcome
        return None
    token, _, arg = raw[1:].partition(" ")
    cmd = token.strip().lower()
    arg = arg.strip()

    if cmd == "fortschritt":
        return Direct(progress_text(ctx.srs, ctx.session))

    if cmd == "fehler":
        return Direct(fehler_text(ctx.srs))

    if cmd == "tag":
        plan = ctx.today_plan() or ctx.build_plan()
        return Direct(render_tag(plan))

    if cmd == "erfolge":
        if not arg:
            return Direct(erfolge_text(ctx))
        return ToLLM(
            user_message=f"Ich habe heute diesen Satz benutzt: „{arg}“",
            system_note=(
                "Lucas hat diesen Satz heute wirklich benutzt und meldet ihn als Erfolg. "
                "Ist der Satz korrekt (A1-A2)? Wenn ja: bestätige kurz und freu dich. "
                "Wenn nein: korrigiere sanft in einer Zeile nach dem Muster "
                "'Richtig: … / Deine Version: …'. Antworte kurz (max. 2 Sätze)."
            ),
            success_phrases=(arg,),
        )

    if cmd == "pause":
        try:
            days = int(arg) if arg else 1
        except ValueError:
            return Direct("Wie viele Tage Pause? Zum Beispiel: /pause 3")
        if not 1 <= days <= MAX_PAUSED_DAYS:
            return Direct("Zwischen 1 und 365 Tagen, bitte. Zum Beispiel: /pause 3")
        ctx.session.pause_until = date.today() + timedelta(days=days)
        return Direct(
            f"Alles klar — ich pausiere bis {ctx.session.pause_until.isoformat()}. 🌙 "
            "Schreib einfach, wenn du zurück bist!"
        )

    if cmd == "quiz":
        if ctx.quiz is not None and not ctx.quiz.finished:
            return Direct("Ein Quiz läuft schon — antworte einfach oder tippe „ende“.")
        if arg:
            try:
                n = int(arg)
            except ValueError:
                return Direct("Wie viele Fragen? Zum Beispiel: /quiz 5")
            if not 1 <= n <= quiz_mod.QUIZ_MAX:
                return Direct(f"Zwischen 1 und {quiz_mod.QUIZ_MAX} Fragen, bitte. Zum Beispiel: /quiz 5")
        else:
            n = quiz_mod.QUIZ_DEFAULT
        ctx.quiz = quiz_mod.start(ctx.srs, n=n)
        intro = (
            f"📚 Los geht's — {n} Fragen zu deinen Vokabeln "
            "(„ende“ beendet das Quiz).\n\n"
        )
        return Direct(intro + ctx.quiz.intro())

    if cmd == "sprechen":
        return Direct(
            "Der Sprachmodus läuft im Terminal: herr-claw sprechen 🎙️ — hier schreiben wir weiter."
        )

    if cmd == "erkläre":
        if not arg:
            return Direct("Was soll ich erklären? Zum Beispiel: /erkläre „umziehen“")
        return ToLLM(
            user_message=f"Erkläre bitte: {arg}",
            system_note=(
                "Lucas bittet um eine Erklärung. Antworte auf ENGLISCH, einfach und kurz "
                "(max. 4 Sätze), mit einem oder zwei Beispielen auf Deutsch (A1-Niveau, "
                "Nomen mit Artikel und Plural)."
            ),
        )

    if cmd == "üben":
        if not arg:
            return Direct("Womit wollen wir üben? Zum Beispiel: /üben Bäckerei")
        ctx.session.current_topic = arg
        system_note = (
            f"Lucas will zum Thema „{arg}“ üben. Beginne die Übung: gib EINEN kurzen "
            "Beispielsatz zum Thema (A1-A2) und stelle EINE einfache Frage an Lucas."
        )
        if ctx.bridge is not None:
            booking = book_topic_session(ctx.bridge, arg)
            if booking:
                system_note += f"\n[System] Buchung über die Bridge: {booking} — erwähne es kurz auf Deutsch."
        return ToLLM(user_message=f"Lass uns über das Thema „{arg}“ üben!", system_note=system_note)

    return Direct(
        "Diesen Befehl kenne ich nicht. Diese kenne ich: " + " · ".join(COMMANDS)
    )
