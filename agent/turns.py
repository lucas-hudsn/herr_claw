"""The tutor brain (P6) — one seam, two implementations, every frontend.

The agent's brain runs INSIDE the NemoClaw sandbox: conversation turns,
command dispatch, SRS/memory state and the §4.1 jobs execute there, and the
host runs only thin frontends (TUI, voice) plus the audited bridge (Apple/
vault). This module is that seam:

- LocalBrain — the brain in-process. Runs as `herr-claw turn` inside the
  sandbox (one process per turn; continuity comes from the ONE state path,
  config.state_dir) and serves as the host's break-glass fallback when the
  sandbox is unreachable.
- SandboxBrain — the host-side thin client: ships each user message to
  `herr-claw turn --json` inside sandbox `my-assistant` via `nemoclaw exec`
  and brings back reply + status.
- AutoBrain — sandbox first, one warned fallback to LocalBrain.

All state effects (history window, active quiz, session counters, memory
journal, vault notes) happen where the brain runs and persist in the ONE
state path — never on the host frontends.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .bridge import BridgeClient, BridgeError, BridgeVault
from .commands import ChatContext, Direct, ToLLM, dispatch, note_user_exchange
from .config import Config, SOUL_PATH, load_config
from .llm import LLMError, Tutor, make_client
from .memory import MemoryState
from .quiz import QuizSession, ensure_seeded
from .scheduling import fetch_today_events, suggest_topic
from .state import SessionState, SrsState
from .tracker import Tracker

MAX_HISTORY_MESSAGES = 12
SESSION_GAP = timedelta(minutes=60)  # idle longer than this closes the session
TURN_JSON_VERSION = 1

_FALLBACK_SOUL = (
    "Du bist Herr Claw, ein geduldiger Deutsch-Tutor für Lucas (A1-A2, Berlin). "
    "Antworte nur auf Deutsch in kurzen, einfachen Sätzen (max. 3-4), ohne Markdown, "
    "denn die Antworten werden laut vorgelesen. Nomen immer mit Artikel und Plural. "
    "Korrigiere Fehler sanft in einer Zeile: 'Richtig: … / Deine Version: …'. "
    "Erkläre auf Englisch nur, wenn Lucas darum bittet."
)


def load_soul(path: Path = SOUL_PATH) -> str:
    try:
        soul = path.read_text(encoding="utf-8").strip()
        if soul:
            return soul
    except OSError:
        pass
    return _FALLBACK_SOUL


def system_prompt_with_memory(memory: MemoryState | None) -> str:
    """SOUL + the memory journal — every content-generating prompt starts
    from this (SPEC §4.6); the memory tailors phrasing/topics only."""
    base = load_soul()
    if memory is None:
        return base
    block = memory.prompt_block()
    return f"{base}\n\n{block}" if block else base


class TurnError(RuntimeError):
    """A turn could not be completed (sandbox unreachable, LLM down)."""


@dataclass(frozen=True)
class TurnResult:
    """One completed tutor turn: the reply plus the status-line facts."""

    reply: str
    speakable: bool = True  # False for command output (printed, never spoken)
    streak: int = 0
    due: int = 0
    topic: str = ""

    def to_json(self) -> dict:
        return {
            "version": TURN_JSON_VERSION,
            "reply": self.reply,
            "speakable": self.speakable,
            "streak": self.streak,
            "due": self.due,
            "topic": self.topic,
        }

    @classmethod
    def from_json(cls, data: dict) -> "TurnResult":
        return cls(
            reply=str(data.get("reply", "")),
            speakable=bool(data.get("speakable", True)),
            streak=int(data.get("streak", 0)),
            due=int(data.get("due", 0)),
            topic=str(data.get("topic", "")),
        )


class LocalBrain:
    """The brain in-process: one dispatch → (tutor) reply per turn() call,
    all state persisted so any later process continues seamlessly."""

    def __init__(
        self,
        cfg: Config | None = None,
        *,
        tutor: Tutor | None = None,
        bridge: BridgeClient | None = None,
        vault=None,  # Vault (host) or BridgeVault (sandbox); None → derive
        max_tokens: int | None = None,
        now_fn=datetime.now,
    ) -> None:
        self.cfg = cfg or load_config()
        self.srs = SrsState(self.cfg.srs_path)
        ensure_seeded(self.srs)
        self.session = SessionState.load(self.cfg.session_path)
        self.memory = MemoryState.load(self.cfg.memory_path)
        if bridge is not None:
            self.bridge = bridge
        else:
            self.bridge = BridgeClient(self.cfg.bridge_url) if self.cfg.bridge_url else None
        if vault is not None:
            self.vault = vault
        elif self.cfg.vault_root is not None:
            from herrclaw_bridge.vault import Vault

            self.vault = Vault(self.cfg.vault_root)  # host fast path (break-glass)
        elif self.bridge is not None:
            self.vault = BridgeVault(self.bridge)  # sandbox path: vault over the bridge
        else:
            self.vault = None
        self.tracker = Tracker(vault=self.vault, srs=self.srs, session=self.session)
        self.ctx = ChatContext(
            srs=self.srs,
            session=self.session,
            tracker=self.tracker,
            bridge=self.bridge,
            memory=self.memory,
        )
        if tutor is not None:
            self.tutor = tutor
        else:
            kwargs = {"max_tokens": max_tokens} if max_tokens else {}
            self.tutor = Tutor(
                make_client(self.cfg.base_url),
                model=self.cfg.model,
                system_prompt=system_prompt_with_memory(self.memory),
                **kwargs,
            )
        self.history = self.session.history[-MAX_HISTORY_MESSAGES:]
        self._now = now_fn
        self._startup_note = self._topic_from_calendar()

    # -- the one public action ---------------------------------------------

    def turn(self, text: str) -> TurnResult:
        self._close_stale_session()
        # a quiz started in another process (cron fire, earlier turn) continues here
        if self.ctx.quiz is None and self.session.active_quiz:
            restored = QuizSession.from_json(self.srs, self.session.active_quiz)
            if restored is not None:
                self.ctx.quiz = restored
        startup_note, self._startup_note = self._startup_note, None

        outcome = dispatch(text, self.ctx)
        if isinstance(outcome, Direct):
            reply, speakable = outcome.text, False  # command output — printed only
        else:
            if isinstance(outcome, ToLLM):
                user_message, system_note = outcome.user_message, outcome.system_note
                extra = outcome.success_phrases
            else:
                user_message, system_note, extra = text, None, ()
            if system_note is None and startup_note:
                system_note = startup_note
            self.history.append({"role": "user", "content": user_message})
            try:
                reply = self.tutor.reply(
                    self.history[-MAX_HISTORY_MESSAGES:], system_note=system_note
                )
            except LLMError as exc:
                self.history.pop()  # keep the window clean; the user simply retries
                raise TurnError(str(exc)) from exc
            self.history.append({"role": "assistant", "content": reply})
            note_user_exchange(self.ctx, user_message, reply, extra_successes=extra)
            speakable = True

        self._persist()
        return TurnResult(
            reply=reply,
            speakable=speakable,
            streak=self.srs.streak.current,
            due=len(self.srs.due()),
            topic=self.session.current_topic,
        )

    def close(self) -> None:
        """End the open session (loop shutdown / poller stop). One-shot
        `turn` processes never call this — the next turn's gap-close does."""
        self.session.last_session_turns = self.session.session_turns
        self.session.last_session_end = self._now().isoformat(timespec="seconds")
        self.tracker.end_session(topic=self.session.current_topic)
        self.srs.save()
        self.session.save()

    # -- internals -----------------------------------------------------------

    def _close_stale_session(self) -> None:
        if not self.session.last_activity:
            return
        try:
            last = datetime.fromisoformat(self.session.last_activity)
        except ValueError:
            return
        if self._now() - last <= SESSION_GAP:
            return
        turns = self.session.session_turns  # capture before end_session resets it
        self.tracker.end_session(topic=self.session.current_topic)
        self.session.last_session_turns = turns
        self.session.last_session_end = self._now().isoformat(timespec="seconds")
        self.srs.save()
        self.session.save()

    def _persist(self) -> None:
        self.session.history = self.history[-MAX_HISTORY_MESSAGES:]
        quiz = self.ctx.quiz
        self.session.active_quiz = quiz.to_json() if quiz is not None and not quiz.finished else {}
        self.session.last_activity = self._now().isoformat(timespec="seconds")
        self.srs.save()
        self.session.save()

    def _topic_from_calendar(self) -> str | None:
        """P2 flourish, now brain-side: when no topic is set and the bridge
        is up, today's calendar suggests the practice topic. Silent
        degradation — the brain must start instantly and identically when
        the bridge is down."""
        if self.bridge is None or self.session.current_topic:
            return None
        try:
            events = fetch_today_events(self.bridge)
        except BridgeError:
            return None
        topic = suggest_topic(events, now=self._now())
        if not topic:
            return None
        self.session.current_topic = topic
        return (
            f"Lucas startet eine neue Session. Aus seinem Kalender heute ergibt sich das "
            f"Thema „{topic}“ — beginne damit: EIN kurzer Satz zum Thema + EINE einfache "
            f"Frage an Lucas (A1-A2)."
        )


class SandboxBrain:
    """Host-side thin client: one `nemoclaw exec` per turn into the sandbox's
    trigger venv. The reply (and status) come back as `turn --json` stdout;
    diagnostics ride stderr."""

    def __init__(self, sandbox: str = "", *, runner=None, timeout_s: float = 150.0) -> None:
        from .cron_sync import SANDBOX_HERR_CLAW, sandbox_name

        self.sandbox = sandbox or sandbox_name()
        self.herr_claw = SANDBOX_HERR_CLAW
        self.timeout_s = timeout_s
        self._runner = runner or self._default_runner

    def turn(self, text: str) -> TurnResult:
        rc, out, err = self._runner(self.sandbox, text, self.timeout_s)
        if rc != 0:
            detail = (err or out).strip().splitlines()
            raise TurnError(detail[-1] if detail else "Sandbox-Turn fehlgeschlagen.")
        try:
            return TurnResult.from_json(json.loads(out))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise TurnError(f"Unerwartete Sandbox-Antwort: {out.strip()[:120]}") from exc

    def close(self) -> None:
        pass  # stateless client — the sandbox owns every bit of state

    def _default_runner(self, sandbox: str, text: str, timeout_s: float) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                [
                    "nemoclaw", sandbox, "exec", "--no-tty",
                    "--timeout", str(int(timeout_s)),
                    "--", self.herr_claw, "turn", "--json",
                ],
                input=text,
                capture_output=True,
                text=True,
                timeout=timeout_s + 30.0,
            )
        except FileNotFoundError as exc:
            raise TurnError("nemoclaw CLI nicht gefunden — Sandbox-Modus benötigt sie.") from exc
        except subprocess.TimeoutExpired as exc:
            raise TurnError(f"Sandbox-Turn nach {timeout_s:.0f}s abgebrochen.") from exc
        return proc.returncode, proc.stdout or "", proc.stderr or ""


class AutoBrain:
    """Sandbox first; if a turn fails there, fall back to the local brain
    (break-glass) with exactly one warning."""

    def __init__(self, cfg: Config | None = None, output_fn=print) -> None:
        self.cfg = cfg or load_config()
        self._output = output_fn
        self._sandbox: SandboxBrain | None = (
            SandboxBrain() if self.cfg.chat_backend != "local" else None
        )
        self._local: LocalBrain | None = None

    def turn(self, text: str) -> TurnResult:
        if self._sandbox is not None:
            try:
                return self._sandbox.turn(text)
            except TurnError as exc:
                self._output(f"⚠︎ Sandbox nicht erreichbar ({exc}) — lokaler Break-Glass-Modus.")
                self._sandbox = None
        if self._local is None:
            self._local = LocalBrain(self.cfg)
        return self._local.turn(text)

    def close(self) -> None:
        if self._local is not None:
            self._local.close()


def make_brain(cfg: Config | None = None, output_fn=print) -> AutoBrain:
    """The frontend default: HERR_CHAT_BACKEND decides sandbox (default) vs
    local (break-glass); AutoBrain adds the runtime fallback."""
    return AutoBrain(cfg, output_fn)


def run_turn_cli(
    message: str | None = None,
    *,
    json_out: bool = False,
    cfg: Config | None = None,
    read_input=None,
    output_fn=print,
) -> int:
    """`herr-claw turn` — one tutor turn, reply on stdout (JSON with --json),
    diagnostics on stderr. Runs inside the sandbox; never closes the session
    (the next turn's gap-close owns that), so state stays continuous."""
    text = (message or "").strip()
    if not text:
        reader = read_input or sys.stdin.read
        text = reader().strip()
    if not text:
        print("Leere Nachricht — nichts zu tun.", file=sys.stderr)
        return 1
    try:
        result = LocalBrain(cfg).turn(text)
    except (TurnError, LLMError) as exc:
        print(f"⚠︎ {exc}", file=sys.stderr)
        return 1
    if json_out:
        output_fn(json.dumps(result.to_json(), ensure_ascii=False))
    else:
        output_fn(result.reply)
    return 0
