"""The stable German slash commands (AGENTS.md — do not rename, no aliases).

P1 implemented the state-only commands (/fortschritt, /fehler, /pause,
/erkläre, /üben topic steering); P4 replaces the /quiz stub with the real
SRS quiz (agent/quiz.py) and answers /sprechen truthfully (P3 shipped it).
While a quiz is active, plain text is graded as an answer and „ende“ quits —
identically in chat, sprechen and Telegram, because all three go through
dispatch().
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from . import quiz as quiz_mod
from .bridge import BridgeClient
from .scheduling import book_topic_session
from .state import SessionState, SrsState
from .tracker import Tracker

COMMANDS = ("/üben", "/quiz", "/fehler", "/fortschritt", "/erkläre", "/pause", "/sprechen")

MAX_PAUSED_DAYS = 365


@dataclass
class ChatContext:
    srs: SrsState
    session: SessionState
    tracker: Tracker
    bridge: BridgeClient | None = None  # None → /üben works, just doesn't book
    quiz: quiz_mod.QuizSession | None = None  # active quiz (P4); None when idle


@dataclass
class Direct:
    """Reply without the LLM (command output)."""

    text: str


@dataclass
class ToLLM:
    """Send a message through the tutor, with optional extra system note."""

    user_message: str
    system_note: str | None = None


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
