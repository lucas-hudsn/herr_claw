"""The P6 in-sandbox Telegram receiver: poller loop (single-chat fence,
brain grading, transport backoff) and the self-healing watchdog (idempotent,
stale pidfile, detached restart). Everything injected — no network."""

import pytest

from agent.poller import run_poller, run_watchdog
from agent.telegram import TelegramError
from agent.turns import TurnError, TurnResult


class FakeTelegram:
    def __init__(self, updates_per_call=None):
        self.sent: list[tuple[str, str]] = []
        self.get_me_calls = 0
        self.updates_per_call = list(updates_per_call or [])
        self.offsets: list[int | None] = []

    def get_me(self):
        self.get_me_calls += 1
        return {"username": "frau_claw_bot"}

    def get_updates(self, offset=None, timeout_s=30.0):
        self.offsets.append(offset)
        if not self.updates_per_call:
            raise KeyboardInterrupt  # end the loop cleanly
        batch = self.updates_per_call.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch

    def send_message(self, chat_id, text):
        self.sent.append((str(chat_id), text))


class FakeBrain:
    def __init__(self, error: Exception | None = None):
        self.texts: list[str] = []
        self.error = error
        self.closed = False

    def turn(self, text):
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return TurnResult(reply=f"Echo: {text}")

    def close(self):
        self.closed = True


def message(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


class Cfg:
    def __init__(self, chat_id="111"):
        self.telegram_chat_id = chat_id


def test_poller_grades_messages_through_the_brain():
    client = FakeTelegram(updates_per_call=[[message(1, "111", "Hallo!"), message(2, "111", "/fortschritt")]])
    brain = FakeBrain()
    code = run_poller(cfg=Cfg(), client=client, brain=brain, output_fn=lambda s: None)
    assert code == 0
    assert brain.texts == ["Hallo!", "/fortschritt"]
    assert client.sent == [("111", "Echo: Hallo!"), ("111", "Echo: /fortschritt")]
    assert brain.closed  # the poller closes the brain (session end)


def test_poller_ignores_foreign_chats():
    client = FakeTelegram(updates_per_call=[[message(1, "999", "fremd"), message(2, "111", "meins")]])
    brain = FakeBrain()
    run_poller(cfg=Cfg(), client=client, brain=brain, output_fn=lambda s: None)
    assert brain.texts == ["meins"]  # the fence held
    assert all(chat == "111" for chat, _ in client.sent)


def test_poller_advances_the_offset():
    client = FakeTelegram(updates_per_call=[[message(7, "111", "eins")], [message(8, "111", "zwei")]])
    run_poller(cfg=Cfg(), client=client, brain=FakeBrain(), output_fn=lambda s: None)
    assert client.offsets == [None, 8, 9]  # confirm-and-advance across long polls


def test_poller_backs_off_after_transport_errors():
    client = FakeTelegram(
        updates_per_call=[
            TelegramError("Timeout"),
            TelegramError("Timeout"),
            [message(1, "111", "klappt")],
        ]
    )
    brain = FakeBrain()
    sleeps: list[float] = []
    run_poller(cfg=Cfg(), client=client, brain=brain, sleep_fn=sleeps.append, output_fn=lambda s: None)
    assert sleeps == [5, 5]  # backed off twice, then recovered
    assert brain.texts == ["klappt"]


def test_poller_reports_brain_errors_to_the_chat():
    client = FakeTelegram(updates_per_call=[[message(1, "111", "Hallo!")]])
    brain = FakeBrain(error=TurnError("Sandbox kaputt"))
    run_poller(cfg=Cfg(), client=client, brain=brain, output_fn=lambda s: None)
    assert client.sent == [("111", "⚠︎ Sandbox kaputt")]


def test_poller_refuses_without_the_single_chat():
    code = run_poller(
        cfg=Cfg(chat_id=""),
        client=FakeTelegram(),
        brain=FakeBrain(),
        output_fn=lambda s: None,
    )
    assert code == 1  # no chat id → no receiver (the fence is load-bearing)


# -- the watchdog ---------------------------------------------------------------------


def test_watchdog_noop_when_poller_healthy(tmp_path):
    pid_path = tmp_path / "telegram.pid"
    pid_path.write_text("123\n")
    calls = []
    code = run_watchdog(
        pid_path=pid_path,
        alive_fn=lambda pid: calls.append(pid) or True,
        spawner=lambda cmd, log, pid: pytest.fail("must not spawn"),
        output_fn=lambda s: None,
    )
    assert code == 0 and calls == [123]


def test_watchdog_restarts_and_writes_pid_when_stale(tmp_path):
    pid_path = tmp_path / "telegram.pid"
    pid_path.write_text("999\n")  # dead pid
    spawned = []

    def spawner(cmd, log_path):
        spawned.append((cmd, log_path))
        return 4242

    code = run_watchdog(
        pid_path=pid_path,
        alive_fn=lambda pid: False,
        spawner=spawner,
        output_fn=lambda s: None,
    )
    assert code == 0
    assert spawned and spawned[0][1] == tmp_path / "telegram.log"
    assert pid_path.read_text().strip() == "4242"


def test_watchdog_recovers_from_a_garbled_pidfile(tmp_path):
    pid_path = tmp_path / "telegram.pid"
    pid_path.write_text("ghost\n")
    code = run_watchdog(
        pid_path=pid_path,
        alive_fn=lambda pid: pytest.fail("garbled pid must not be probed"),
        spawner=lambda cmd, log: 7,
        output_fn=lambda s: None,
    )
    assert code == 0 and pid_path.read_text().strip() == "7"
