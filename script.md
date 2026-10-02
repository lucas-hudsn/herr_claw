# Herr Claw — Setup / Test / Shutdown Runbook

Step by step: bring everything up, test every function, shut it all down
again. Each block ends with a **❓ Prompt** — confirm it before moving on.
Run all commands from the repo root
(`/Users/lucashud/Desktop/projects/herr_claw`) unless noted otherwise.

Known gotchas (verified 2026-10-02):
- A sandbox start can fail once with `still being created by onboarding` —
  re-check `status` after 30 s, it is usually `Ready` then.
- The Telegram token placeholder in cron entries must be the **full**
  gateway-issued form (`openshell:resolve:env:v8392…_TELEGRAM_BOT_TOKEN`,
  see `TELEGRAM_TOKEN_PLACEHOLDER` in `agent/cron_sync.py`) — the short form
  returns gateway 500s and the loop never starts.
- The `Deutsch Lernen` calendar must **exist** in Calendar.app —
  auto-creation fails (`EKErrorDomain Code=17`).

---

## Phase 0 — Prerequisites (one-time)

```sh
# Docker Desktop running?
docker info >/dev/null 2>&1 && echo OK-DOCKER
# Deps + .env (fill in the values; .env is never read/logged by tooling)
uv sync --all-extras && cp -n .env.example .env
```

> ❓ Prompt: Docker OK? `.env` filled in (`NVIDIA_API_KEY`,
> `TELEGRAM_BOT_TOKEN`, `HERR_TELEGRAM_CHAT_ID`)? Calendar
> `Deutsch Lernen` + Reminders list `Deutsch` exist? → `continue`

## Phase 1 — Start gateway + sandbox

```sh
# Gateway (brew service; no trust workaround needed this way):
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/sh.brew.openshell.plist
ps aux | grep openshell-gateway | grep -v grep   # must show ONE line
nemoclaw my-assistant start                      # on onboarding error: wait 30 s
nemoclaw my-assistant status | grep -E "Phase"   # must show "Phase: Ready"
nemoclaw my-assistant doctor                     # Summary: healthy (1 telegram warning is OK —
                                                 # the runtime's own Telegram polling stays OFF)
```

> ❓ Prompt: `Phase: Ready`? Doctor healthy? → `continue`

## Phase 2 — Sync + register cron

```sh
uv sync --all-extras
uv run herr-claw install-cron   # 4 jobs, NO chat-id warning (else check .env)
nemoclaw my-assistant exec -- openclaw cron list --json | grep -o '"name":"[^"]*"' | sort -u
# Expected: herr-claw-nudge, herr-claw-quiz, herr-claw-recap, herr-claw-telegram-watchdog
```

> ❓ Prompt: 4 jobs present (08:00 / 12:30 / 20:00 / every 5 min)? → `continue`

## Phase 3 — Start the bridge (host: the ONE Apple/vault door)

```sh
HERR_BRIDGE_ALLOWED_HOSTS="host.openshell.internal:8765" \
HERR_BRIDGE_ALLOWED_ORIGINS="host.openshell.internal:8765" \
nohup uv run herr-claw bridge > /tmp/herrclaw-bridge.log 2>&1 &
sleep 8; tail -2 /tmp/herrclaw-bridge.log   # "session manager started"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/mcp  # 400 = up (handshake expects a session)
```

> ❓ Prompt: Bridge log shows start, port answers? → `continue`

## Phase 4 — Function tests

### T1 — CLI alive?
```sh
uv run herr-claw --help >/dev/null && echo OK-CLI
```
> ❓ Prompt: OK-CLI? → `continue`

### T2 — Host break-glass turn (no sandbox dependency)
```sh
HERR_CHAT_BACKEND=local uv run herr-claw turn "/fortschritt"  # German streak/SRS reply
```
> ❓ Prompt: Progress reply received? → `continue`

### T3 — Sandbox brain: direct + LLM-backed
```sh
nemoclaw my-assistant exec -- /sandbox/herrclaw/.venv/bin/herr-claw turn "/fortschritt"
nemoclaw my-assistant exec -- /sandbox/herrclaw/.venv/bin/herr-claw turn "/erkläre Brot"
```
> ❓ Prompt: Both replies in German (2nd with LLM explanation)? → `continue`

### T4 — Bridge read from the sandbox (`/tag` is write-free)
```sh
nemoclaw my-assistant exec -- /sandbox/herrclaw/.venv/bin/herr-claw turn "/tag"
tail -3 ~/.herr-claw/audit.log   # must show calendar.freebusy "allowed": true
```
> ❓ Prompt: Phrase plan shown? Audit shows `calendar.freebusy allowed`? → `continue`

### T5 — Telegram loop alive?
```sh
WID=$(nemoclaw my-assistant exec -- openclaw cron list --json | grep -o '"id":"[^"]*","name":"herr-claw-telegram-watchdog"' | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
nemoclaw my-assistant exec -- openclaw cron run "$WID"   # fire the watchdog
sleep 30
nemoclaw my-assistant exec -- sh -c 'P=$(cat /sandbox/herrclaw/state/telegram.pid); ps -p $P -o pid,etime,cmd | tail -1'
# Expected: .../herr-claw telegram-loop, ELAPSED growing, log without fresh 500s:
nemoclaw my-assistant exec -- tail -3 /sandbox/herrclaw/state/telegram.log
```
> ❓ Prompt: Loop process running (still there after another 60 s)? No fresh
> `Telegram-API-Fehler (500)` lines? → `continue`

### T6 — Day loop with real side effects (`trigger nudge`)
This really books: Reminder in list `Deutsch`, 15-min event in calendar
`Deutsch Lernen`, Morgen-Brief via Telegram + daily-note append. Only
meaningful in the morning; after 12:30 the quiz catch-up comes back empty
(known edge: the quiz is then marked "done" without asking).
```sh
nemoclaw my-assistant exec -- sh -c 'cd /sandbox/herrclaw && HERR_TELEGRAM_CHAT_ID=8474363226 /sandbox/herrclaw/.venv/bin/herr-claw trigger nudge'
tail -5 ~/.herr-claw/audit.log  # reminders.add + calendar.add/vault.append "allowed": true
```
> ❓ Prompt: Reminder in Apple Reminders (`Deutsch`)? Calendar event present?
> Morgen-Brief arrived on the phone? Daily note extended? → `continue`

### T7 — Phone end-to-end (the real proof)
From the phone, message `@frau_claw_bot`: `/fortschritt`.
> ❓ Prompt: Does the sandbox loop answer in German? Optional quiz check:
> start `/quiz 2` on the phone, answer one question. → `continue`

## Phase 5 — Shutdown (everything down)

```sh
# 1. Host bridge
pkill -f "herr-claw bridge"; sleep 2
curl -s -m 5 -o /dev/null http://127.0.0.1:8765/mcp || echo "BRIDGE DOWN"
# 2. Sandbox (state is preserved)
nemoclaw my-assistant stop
docker ps | grep -i assistant || echo "SANDBOX DOWN"
# 3. OpenShell gateway
launchctl bootout gui/$(id -u)/sh.brew.openshell; sleep 2
ps aux | grep openshell-gateway | grep -v grep || echo "GATEWAY DOWN"
# Docker Desktop intentionally stays ON (the sandbox needs it next start)
```

> ❓ Prompt: All four checks DOWN? → `done` ✅

Next time: start again at **Phase 1**.
