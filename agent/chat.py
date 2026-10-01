"""`herr-claw chat` — the text TUI loop (P1).

Read–reply loop against Nemotron 3 with SOUL.md as system prompt, rolling
history, correction extraction, and persistence: state/ always, Obsidian
(via the bridge's scoped vault) when HERR_VAULT is set.
"""

from __future__ import annotations

from datetime import datetime

from .bridge import BridgeClient, BridgeError
from .commands import ChatContext, Direct, ToLLM, dispatch
from .config import Config, SOUL_PATH, load_config
from .llm import LLMError, Tutor, make_client, parse_correction
from .scheduling import fetch_today_events, suggest_topic
from .state import SessionState, SrsState
from .tracker import Tracker

MAX_HISTORY_MESSAGES = 12

_FALLBACK_SOUL = (
    "Du bist Herr Claw, ein geduldiger Deutsch-Tutor für Lucas (A1-A2, Berlin). "
    "Antworte nur auf Deutsch in kurzen, einfachen Sätzen (max. 3-4), ohne Markdown, "
    "denn die Antworten werden laut vorgelesen. Nomen immer mit Artikel und Plural. "
    "Korrigiere Fehler sanft in einer Zeile: 'Richtig: … / Deine Version: …'. "
    "Erkläre auf Englisch nur, wenn Lucas darum bittet."
)

BANNER = """\
Herr Claw 🤖 — dein Deutsch-Tutor (A1–A2)
Befehle: /üben [Thema] · /quiz · /fehler · /fortschritt · /erkläre [Wort] · /pause [Tage] · /sprechen
Beenden: Strg-D
"""


def load_soul(path=SOUL_PATH) -> str:
    try:
        soul = path.read_text(encoding="utf-8").strip()
        if soul:
            return soul
    except OSError:
        pass
    return _FALLBACK_SOUL


def run_chat(
    cfg: Config | None = None,
    tutor: Tutor | None = None,
    input_fn=input,
    output_fn=print,
) -> int:
    cfg = cfg or load_config()
    srs = SrsState(cfg.srs_path)
    session = SessionState.load(cfg.session_path)
    vault = None
    if cfg.vault_root:
        from herrclaw_bridge.vault import Vault

        vault = Vault(cfg.vault_root)
    else:
        output_fn("⚠︎ HERR_VAULT ist nicht gesetzt — Obsidian-Notizen sind deaktiviert.")
    if tutor is None:
        tutor = Tutor(make_client(cfg.base_url), model=cfg.model, system_prompt=load_soul())
    bridge = BridgeClient(cfg.bridge_url) if cfg.bridge_url else None
    tracker = Tracker(vault=vault, srs=srs)
    ctx = ChatContext(srs=srs, session=session, tracker=tracker, bridge=bridge)

    startup_note = _topic_from_calendar(bridge, session, output_fn)

    output_fn(BANNER)
    history: list[dict[str, str]] = []
    while True:
        try:
            raw = input_fn("Du: ")
        except (EOFError, KeyboardInterrupt):
            output_fn("")
            break
        raw = raw.strip()
        if not raw:
            continue

        outcome = dispatch(raw, ctx)
        if isinstance(outcome, Direct):
            output_fn(f"Herr Claw: {outcome.text}")
            continue
        if isinstance(outcome, ToLLM):
            user_message, system_note = outcome.user_message, outcome.system_note
        else:
            user_message, system_note = raw, None
        if system_note is None and startup_note:
            system_note, startup_note = startup_note, None

        history.append({"role": "user", "content": user_message})
        try:
            reply = tutor.reply(history[-MAX_HISTORY_MESSAGES:], system_note=system_note)
        except LLMError as exc:
            output_fn(f"⚠︎ {exc}")
            break
        history.append({"role": "assistant", "content": reply})
        tracker.note_exchange()
        srs.add_message()
        srs.touch_day()
        output_fn(f"Herr Claw: {reply}")

        correction = parse_correction(reply)
        if correction:
            fix, example = correction
            tracker.log_correction(example=example, fix=fix)

    tracker.end_session(topic=session.current_topic)
    session.last_session_turns = tracker.session_turns
    session.last_session_end = datetime.now().isoformat(timespec="seconds")
    srs.save()
    session.save()
    if tracker.session_turns:
        output_fn("Tschüss! Bis zum nächsten Mal. 👋")
    return 0


def _topic_from_calendar(bridge: BridgeClient | None, session: SessionState, output_fn=print) -> str | None:
    """P2 flourish: when no topic is set and the bridge is up, today's
    calendar suggests the practice topic. Silent degradation — the TUI must
    start instantly and identically when the bridge is down."""
    if bridge is None or session.current_topic:
        return None
    try:
        events = fetch_today_events(bridge)
    except BridgeError:
        return None
    topic = suggest_topic(events, now=datetime.now())
    if not topic:
        return None
    session.current_topic = topic
    output_fn(f"📅 Aus deinem Kalender: heute üben wir „{topic}“.")
    return (
        f"Lucas startet eine neue Session. Aus seinem Kalender heute ergibt sich das "
        f"Thema „{topic}“ — beginne damit: EIN kurzer Satz zum Thema + EINE einfache Frage an Lucas (A1-A2)."
    )
