# DEPLOYMENT — Herr Claw on a Mac mini, as an OpenClaw (NemoClaw/OpenShell)

This is the build book for the submission's primary mode. One script does the
whole build: [`build-deployment.sh`](build-deployment.sh). This document
explains what it does, why each step exists, and what a successful run looks
like with **real output** from the live deployment (2026-10-02).

---

## Problem statement

> Build a long-running agent: it should work for you every day, on its own,
> learn who you are, and stay governable — without a resident cloud VM.

Four concrete constraints fall out of that:

1. **Long-running, not long-hosted.** The agent must keep firing jobs every
   day (morning/midday/evening) unattended, accumulate memory across days —
   but "always-on" should not mean "always paying." The target hardware is a
   Mac mini or an old MacBook you already own.
2. **The agent must touch the real world** — Apple Calendar, Reminders, an
   Obsidian vault, Telegram — and Apple automation only exists on macOS.
3. **It must stay governable.** An agent that can read your calendar and
   write to your notes needs hard fences: no secrets in agent scope, no
   un-audited writes, deny-by-default networking.
4. **It must be reproducible.** A deployment that only exists on one machine
   because "it was set up once by hand" is not a deployment.

## Solution

Deploy the agent as an **OpenClaw inside a NemoClaw/OpenShell sandbox** on
the Mac itself — and since P6 the agent's *brain* lives there too — and
make the day *self-healing*:

```
┌ macOS host — thin frontends + the ONE capability door ────────────────┐
│  TUI `herr-claw chat` / voice `sprechen`                              │
│    rendering + macOS audio only — every user message is ONE           │
│    `nemoclaw exec … herr-claw turn` into the sandbox                  │
│  herrclaw_bridge (MCP, 127.0.0.1:8765 — 7 audited tools)              │
│    reminders.* → Apple Reminders (list `Deutsch`)                     │
│    calendar.*  → Apple Calendar (`Deutsch Lernen`)                    │
│    vault.*     → Obsidian (3 scoped dirs, append)                     │
│  audit: ~/.herr-claw/audit.log — every allow AND deny                 │
│  secrets: gateway-held (host .env is dev/break-glass fallback only)   │
└───────────────▲──────────────────────────────────────────────────────┘
                │ sandbox dials the bridge for Apple/vault ONLY
                │ host.openshell.internal:8765 · POST/GET/DELETE /mcp only
                │ allowed_ips (private ranges) · binaries pin: trigger venv
                │ + curl + the OpenClaw runtime itself (policy v11)
┌ OpenShell sandbox `my-assistant` (Docker on the same Mac) — THE AGENT ┐
│  OpenClaw agent runtime — cron = the ONE scheduler (schedule.yaml)    │
│    jobs: fire `herr-claw trigger <job>` → job body executes HERE      │
│    telegram-watchdog: keeps the ONE conversation receiver alive       │
│  `herr-claw turn` — the brain (agent/turns.py): one process per       │
│    TUI/voice/Telegram message; LLM via inference.local (gateway)      │
│  telegram-loop — long-poll getUpdates → same brain → sendMessage      │
│  state: /sandbox/herrclaw/state — srs.json · session.json (history,   │
│    active quiz, session counters) · memory.md — ONE state path        │
│  filesystem: /sandbox + /tmp only; the vault only via the bridge      │
│  egress deny-by-default: inference.local · api.telegram.org · bridge  │
└──────────────────────────────────────────────────────────────────────┘
```

The division of labor is the security model:

- **The sandbox schedules, thinks, and executes.** The OpenClaw cron
  (registered from the one `agent/schedule.yaml` by `herr-claw
  install-cron`) fires `herr-claw trigger <job>` inside a vendored,
  offline Python venv — and the job body runs right there, against
  `/sandbox/herrclaw/state`. The same venv serves every conversation:
  `herr-claw turn` (one process per TUI/voice/Telegram message) and
  `telegram-loop` (the ONE getUpdates receiver, kept alive by a
  `*/5` watchdog cron entry). The sandbox is the agent.
- **The host decides what a bridge call may do.** Apple Calendar/
  Reminders and the Obsidian vault exist only on macOS, so they stay
  behind the bridge: 7 fixed tools, vault writes append-only inside
  `Deutsch/`, `Daily notes/`, `Weeks/` — and every allow and deny is
  audited to `~/.herr-claw/audit.log`. Sandbox vault writes go through
  the same tools (`BridgeVault` adapter); the vault never leaves the host.
- **The policy layer decides who may ask at all.** Egress is
  deny-by-default with exactly three doors: gateway-routed inference
  (`inference.local` — no key value in the sandbox), Telegram (token
  resolved at egress — the sandbox only ever sees an
  `openshell:resolve:env:` placeholder), and the bridge endpoint,
  caller-pinned by binary path to the trigger venv, curl (smoke tests),
  and the OpenClaw runtime itself. Everything else gets an OCSF
  `DENIED` line in the sandbox policy log.
- **Failures heal themselves.** A trigger retries an unreachable bridge
  (3×, 15 s apart — transport failures only; policy denials are never
  retried). Every fire first **catches up earlier jobs whose fire failed
  earlier today**: if 08:00 is lost, the 12:30 fire heals the day (dedup
  via `session.json` makes that idempotent — a job can never run twice
  on one day). And if the Telegram receiver dies, the watchdog brings
  it back within five minutes.

What the sandbox deliberately does **not** have: your secrets (no key
value, only gateway placeholders), Apple access except through the
audited bridge, package-registry egress (the venv is vendored from the
host as linux-aarch64 wheels), and anything beyond `/sandbox` + `/tmp`
writable.

---

## Prerequisites

- A Mac (Apple Silicon) with Docker Desktop — this is NemoClaw's own
  dependency; the project ships no Dockerfiles of its own.
- **NemoClaw, downloaded from NVIDIA first (one-time):**
  `curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash` installs the
  `nemoclaw` + `openshell` CLIs (with the OpenClaw runtime; Docker Desktop
  comes with it). Then `nemoclaw onboard` once — it creates the gateway and
  the `my-assistant` sandbox. Docs:
  [docs.nvidia.com/nemoclaw](https://docs.nvidia.com/nemoclaw).
- Python 3.14+ with [`uv`](https://docs.astral.sh/uv/).
- A `.env` in the repo (never committed, never printed) defining:
  `NVIDIA_API_KEY`, `TELEGRAM_BOT_TOKEN`, `HERR_TELEGRAM_CHAT_ID`,
  `HERR_VAULT`, `HERR_BRIDGE_ALLOWED_HOSTS=host.openshell.internal:8765`,
  `HERR_BRIDGE_ALLOWED_ORIGINS=host.openshell.internal:8765`.
  Names are documented in [`.env.example`](../.env.example).

## Build — step by step

```sh
nemoclaw-blueprint/build-deployment.sh            # the whole build, verified
nemoclaw-blueprint/build-deployment.sh --smoke    # + one REAL end-to-end trigger fire
```

What each phase does and why it exists:

| # | Phase | What it does | Why |
|---|-------|--------------|-----|
| 0 | preflight | Checks CLIs, Docker, the sandbox record; reminds you of the required `.env` key **names** (never their values) | Fail at second zero, not at minute five |
| 1 | build | `uv build --wheel` → one pure-python `herr_claw` wheel | The sandbox gets an artifact, not a checkout |
| 2 | vendor | Downloads linux-aarch64 wheels for the **core** deps only (bridge/voice extras are macOS-only and excluded) | The sandbox never talks to pypi — no package egress exists or is needed |
| 3 | sandbox | `nemoclaw my-assistant start` | Boots the OpenClaw runtime + OpenShell policy inside Docker |
| 4 | upload | App wheel → `/sandbox/herrclaw-wheels/`, vendored wheels ditto, source (`agent/`, `herrclaw_bridge/`, `main.py`, `pyproject.toml`, `seed/`) → `/sandbox/herrclaw/`, then writes `/sandbox/herrclaw/.env` (non-secret endpoints + the token PLACEHOLDER) | The brain needs exactly these; `voice/` is deliberately **not** uploaded (macOS-only frontend, never the brain) |
| 5 | install | Extracts the wheel(s) into the venv's `site-packages` (the uv-created venv has no pip), writes the `herr-claw` console script by hand | Offline install with zero tooling assumptions inside the sandbox |
| 6 | policy | Applies `policy-additions.yaml` (bridge endpoint, `POST/GET/DELETE /mcp` only); prints the recipe for the caller-pin increments | NemoClaw's merge normalizes `allowed_ips`/`binaries` away, so the pin goes through a full `openshell policy set` per the yaml header |
| 7 | bridge + cron | Starts the host bridge if not up (with the alias allowlist — otherwise alias requests get HTTP 421), then `herr-claw install-cron` | The bridge is the only Apple/vault door; the cron inside the sandbox is the ONE scheduler (+ the telegram watchdog entries) |
| 8 | verify | Policy version, `nemoclaw doctor`, bridge reachability; `--smoke` additionally fires one real job end to end | Trust, then verify |

## Example — a real run

Today's deployment healed itself on camera. The 08:00 cron fires had failed
(the host bridge was down after a Docker outage — the sandbox OCSF log shows
the trigger was **allowed** by policy but the upstream connect failed):

```
FORWARD_L7 allow POST host.openshell.internal:8765/mcp        ← policy: ALLOWED
FORWARD upstream connect failed ... Network unreachable       ← bridge was down
```

Then one command from inside the sandbox (exactly what the cron runs):

```
$ /sandbox/herrclaw/.venv/bin/herr-claw trigger quiz
Job „quiz“ erledigt.
⏰ Nachgeholt: „nudge“ — erledigt
```

…which means: the quiz fire **caught up the lost morning** — the nudge ran
first (calendar read → Reminder → real event booked → Telegram push → daily
note), then the quiz went out. Host-side audit trail for that one fire (the
Apple/vault calls; since P6 the job body itself runs in the sandbox, so the
audit shows exactly the bridge-crossing calls and nothing else):

```
2026-10-02T12:03:00+02:00 calendar.add  Deutsch Lernen: Deutsch Lernen (15m) @ 2026-10-02T12:15  allowed: True
2026-10-02T12:03:01+02:00 vault.append  Daily notes/2026-10-02-deutsch.md                        allowed: True
```

And `/sandbox/herrclaw/state/session.json` afterwards:
`"jobs_done": {"nudge": "2026-10-02", "quiz": "2026-10-02"}` — re-running
the same trigger is a safe no-op:

```
$ /sandbox/herrclaw/.venv/bin/herr-claw trigger quiz
Job „quiz“ lief heute schon — übersprungen (Dedup über session.json).
```

The sandbox agent's own tool path (policy v11) probes clean too:

```
$ openclaw mcp probe herrclaw-bridge
MCP probe (/root/.openclaw/openclaw.json):
- herrclaw-bridge: 7 tools, resources, prompts
```

## Verification checklist

- `openshell policy get my-assistant` → policy version (≥ 11), and
  `--full -o json` shows the binaries pin incl. the OpenClaw runtime.
- `nemoclaw my-assistant doctor` → no `fail` outside the known phase caveat.
- `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/mcp` → 4xx
  (listening; 421 with a foreign Host header means the alias allowlist env
  is missing on the bridge process).
- `~/.herr-claw/audit.log` → one JSON line per bridge call, allow *and* deny.
- Sandbox OCSF log (`/var/log/openshell-ocsf.*.log`) → `ALLOWED` lines for
  pinned binaries, `DENIED ("binary ... not allowed")` for everything else.
- `--smoke` → the German result text comes back through the whole chain.
- P6 additions: one thin-client turn end to end —
  `echo "Hallo!" | nemoclaw my-assistant exec --no-tty -- /sandbox/herrclaw/.venv/bin/herr-claw turn --json`
  → a JSON reply (proves inference.local + state dir); and the placeholder
  token resolves — `/sandbox/herrclaw/.venv/bin/herr-claw telegram-loop`
  starts (getMe OK) and answers only `HERR_TELEGRAM_CHAT_ID`.
- `/sandbox/herrclaw/state/` → `srs.json` + `session.json` after the first
  turn (the ONE state path; the host repo `state/` stays untouched in the
  primary mode).

## Troubleshooting

- **Sandbox phase stuck at `Error`** (start/stop/exec/recover all refuse:
  *"sandbox must be Stopped to start (current phase: Error)"*): the container
  itself can be perfectly healthy — check `docker ps` and the in-sandbox
  logs (`/var/log/openshell-ocsf.*`, `/var/log/openshell.*`). Recovery that
  worked here: bring Docker up, restart the OpenShell gateway
  (`launchctl kickstart -k gui/$(id -u)/sh.brew.openshell`), then a plain
  `docker restart` of the container; admin via
  `docker exec <container> …` works while the phase gate doesn't. A full
  host reboot (Docker + gateway booting together) is the clean fix.
- **HTTP 421 from the bridge** with the alias host: the bridge process was
  started without `HERR_BRIDGE_ALLOWED_HOSTS`/`_ORIGINS` — restart it with
  them set (the script does this for you).
- **`trigger` says the bridge is off**: `HERR_BRIDGE_URL` must be
  `http://host.openshell.internal:8765/mcp` inside the sandbox (the sandbox
  `.env` carries it; cron entries also set it via `--command-env`).
- **Thin-client turns fail with `TurnError`**: the sandbox `.env` is missing
  or stale — rerun the build script (phase 4 rewrites it), or check
  `nemoclaw my-assistant exec -- cat /sandbox/herrclaw/.env`. The TUI falls
  back to the local break-glass brain with one warning when this happens.
- **Telegram 409 Conflict in `telegram-loop`**: another getUpdates poller is
  running (a second `telegram-loop`, or the OpenClaw runtime's own Telegram
  channel). Herr Claw's loop is the ONE receiver — turn the other one off.
- **A deny you didn't expect**: that's the system working. Find the reason
  in the OCSF log (`binary ... not allowed` → add the binary to the pin in
  the yaml header recipe; tool denial → the German error text explains which
  allowlist refused).

## Deliberately kept (i.e., why this isn't "smaller")

- **NVIDIA's onboard policy presets** (`brew`, `huggingface`, `npm`,
  `pypi`, `clawhub`, `openclaw-*`, `telegram`, `managed_inference`) are the
  OpenClaw runtime's own baseline — the runtime checks pricing/docs/market
  and serves inference through them. Herr Claw adds exactly **one** door
  (the bridge) and removes none of the runtime's: trimming the baseline
  would break the claw we're deploying, not fence it tighter.
- **`agent/voice/`, the TUI, the seed list** are product surface (P1–P3).
  Since P6 the *seed list* and the *brain* live in the sandbox; the TUI and
  voice remain host-side frontends (rendering + macOS audio only) and never
  enter it.
