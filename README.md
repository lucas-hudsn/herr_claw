# Herr Claw 🇩🇪

A long-running, sandboxed **German-tutor agent** for the NVIDIA Claw Agent
Challenge — for anyone at A1–A2 who lives in Apple Calendar, Reminders and
Obsidian and wants a daily five-minute German habit. Herr Claw chats with you
in simple German (text or voice), corrects your mistakes gently, quizzes you
with spaced repetition, logs your progress to Obsidian, and runs its own day
on a scheduler: morning nudge, midday micro-quiz, evening recap — nudging you
on Telegram.

Built for macOS (Apple Silicon only, by design), Python 3.14, managed with
[`uv`](https://docs.astral.sh/uv/). Inference runs on
**NVIDIA Nemotron 3** (`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`)
via the OpenAI-compatible endpoint at `integrate.api.nvidia.com`.

## Status — what works today

- **Text chat TUI** (`herr-claw chat`): a German-only tutor loop with a
  persona prompt (`agent/SOUL.md`), rolling history, and soft corrections in
  the fixed template `Richtig: … / Deine Version: …`.
- **Voice mode** (`herr-claw sprechen`): push-to-talk (space/enter) →
  mlx-whisper `base` (language locked to `de`, 16 kHz mono, silence-stopped
  ≤5 s) → Nemotron reply → `say -v Anna` (markdown and emoji stripped before
  speaking). The MacBook mic is picked explicitly — iPhone Continuity ghost
  mics are skipped — and the per-turn latency is printed live against the
  <5 s budget. Typed commands still work next to the mic; Ctrl-C quits and
  saves state.
- **German slash commands**: `/üben`, `/quiz`, `/fehler`, `/fortschritt`,
  `/erkläre`, `/pause`, `/sprechen` — identical behavior in the TUI, voice
  mode, and Telegram because all three go through one `dispatch()`.
  `/erkläre` and `/üben` steer the tutor via system notes; the rest are
  answered directly from local state.
- **`/üben` books real practice time** (with the bridge running): it checks
  today's calendar for a free 15-minute slot (quarter-hour grid, 08:00–22:00,
  never overlapping real events), creates an Apple Reminder
  „Deutsch: 5-Min Chat — Thema …" in the `Deutsch` list, and puts a
  „Deutsch Lernen (15m)" event into the `Deutsch Lernen` calendar. If the
  bridge is down, the topic is still set and the tutor says so honestly.
- **The day's topic comes from your calendar**: when a chat starts with no
  topic set, upcoming calendar titles are mapped to A1 practice themes
  (Anmeldung → Behörden, Bäckerei → Beim Bäcker, Gym → Sport, …). No
  keyword match → no flourish — Herr Claw never invents a calendar
  connection that isn't there.
- **Vocabulary quiz** (`/quiz [n]`): every question is generated from
  stored data only — the 100-word A1 seed list
  (`seed/vocab_a1_100.csv`: food, apartment, small talk, Berlin daily life)
  plus words already in the SRS. The LLM never invents quiz content.
  Five question types (DE→EN, EN→DE, cloze, article der/die/das, plural),
  nouns always shown with article + plural, lenient answer checking
  (case/umlaut/punctuation). Every answer updates the SM-2-lite schedule:
  a hit levels up, a miss drops a level and counts. `„ende"` quits early.
- **The daemon** (`herr-claw daemon`) is the ONE scheduler, configured only
  by `agent/schedule.yaml`: **08:00** morning nudge — read the calendar,
  derive the day's topic (calendar keyword first, else the weakest vocab's
  theme), book a free 15-minute slot (Reminder + Calendar event via the
  bridge), send „Guten Morgen! ☕" to Telegram; **12:30** micro-quiz —
  3 questions from your weakest vocab, only if the morning nudge ran;
  **20:00** recap — append the daily note + `Deutsch/progress.md` to
  Obsidian, mark the practice reminder complete, confirm on Telegram.
  Job dedup lives in `session.json` (a restart never re-fires a job),
  jobs missed earlier today catch up oldest-first on startup, `/pause`
  is respected, and a failed job retries without killing the loop.
- **Telegram**: the daemon long-polls the Bot API (`api.telegram.org` only,
  stdlib client) and answers the same German commands from your phone.
  It answers exactly ONE chat (`HERR_TELEGRAM_CHAT_ID`); other chats are
  logged and ignored, never answered. A restart skips the update backlog,
  so old commands never re-fire.
- **MCP bridge** (`herr-claw bridge`): exposes seven tools over
  Streamable-HTTP MCP on `127.0.0.1:8765` — `reminders.{list,add,complete}`
  (AppleScript, pinned to the `Deutsch` list), `calendar.{freebusy,add}`
  (EventKit, writes pinned to the `Deutsch Lernen` calendar, AppleScript
  fallback only if EventKit is unusable), and `vault.{read,append}`. Agent
  code never touches `osascript` or EventKit directly — the bridge client
  (`agent/bridge.py`) is the only path, so allowlist enforcement and audit
  live in exactly one place. The bridge needs no secrets, only `HERR_VAULT`.
- **Learning state** in `state/`: mistake log (deduplicated with counts),
  daily streak, session/message/correction totals, and the SM-2-lite spaced
  repetition engine (L1→1d, L2→3d, L3→7d, L4→14d, L5→30d; a miss drops one
  level). Both `state/srs.json` and `state/session.json` are written
  atomically; a corrupt `srs.json` is backed up, never silently dropped.
- **Obsidian logging** through the bridge's scoped vault: append-only writes
  to `Deutsch/progress.md`, `Deutsch/fehler.md`, and
  `Daily notes/YYYY-MM-DD-deutsch.md`. If `HERR_VAULT` is unset, chat still
  works and only `state/` persists.
- **Audit trail**: every vault, Reminders, and Calendar call — allowed _and_
  denied — lands as one JSON line in `~/.herr-claw/audit.log`.
- **Robust inference**: `thinking` disabled on every call (reasoning mode
  measured 9–54 s vs ~1.4 s without), retry with backoff on transient
  free-tier 503s, friendly German error messages, and reply sanitization
  that strips reasoning blocks and markdown so answers stay short and
  speakable (they will be read aloud).

Not yet: the Sunday weekly review and the demo video. The sandbox-in
deployment (OpenClaw cron scheduler, OpenShell policy with the bridge
endpoint + caller pin, vendored trigger venv) is live — see Deployment above.

## Requirements

- macOS on Apple Silicon (uses `say`, Apple Reminders/Calendar, mlx-whisper)
- Python 3.14+ and [`uv`](https://docs.astral.sh/uv/)
- An NVIDIA Build API key (https://build.nvidia.com)
- Optional: a microphone for voice mode, an Obsidian vault for progress
  notes, and a Telegram bot token + your chat id for the daemon's nudges
- For the bridge: macOS Automation permission for Reminders + Calendar, and
  EventKit calendar access (first call per context triggers the prompt once)
- For the sandbox-in deployment: [NemoClaw](https://docs.nvidia.com/nemoclaw)
  with the OpenClaw runtime (installs Docker Desktop as its own dependency)
  — `nemoclaw onboard` sets it up

## Setup

```sh
uv sync --all-extras    # create venv and install dependencies (incl. voice + bridge extras)
cp .env.example .env    # then fill in the values
```

Environment variables (documented in `.env.example`; `.env` is gitignored
and holds live secrets — never commit it):

| Variable                | Purpose                                                                                               |
| ----------------------- | ----------------------------------------------------------------------------------------------------- |
| `NVIDIA_API_KEY`        | NVIDIA Build API key for Nemotron inference (required)                                                |
| `TELEGRAM_BOT_TOKEN`    | Telegram bot token for the daemon (`herr-claw daemon`)                                                |
| `HERR_TELEGRAM_CHAT_ID` | The single allowed Telegram chat — the only one the bot answers                                       |
| `HERR_VAULT`            | Path to your Obsidian vault; unset disables vault tools/writes                                        |
| `HERR_BRIDGE_URL`       | MCP bridge endpoint; empty → `http://127.0.0.1:8765/mcp`, `off` disables bridge calls                 |
| `HERR_BRIDGE_ALLOWED_HOSTS` / `HERR_BRIDGE_ALLOWED_ORIGINS` | Extra Host/Origin values the bridge accepts under a non-localhost name (sandbox-in deployment; see below) |
| `HERR_NEMOCLAW_SANDBOX` | NemoClaw sandbox for the OpenClaw-cron scheduler (default `my-assistant`)                             |
| `HERR_MODEL`            | Optional model override                                                                               |
| `HERR_NVIDIA_BASE_URL`  | Optional endpoint override                                                                            |
| `HERR_WHISPER_MODEL`    | Whisper model for voice mode (default `base` — never `tiny`, too weak for German)                     |
| `HERR_MIC_DEVICE`       | Optional input device override (name substring or index); auto-pick skips iPhone/iPad Continuity mics |

## Deployment — OpenClaw via NemoClaw/OpenShell (the primary mode)

Herr Claw deploys as an **OpenClaw agent inside a NemoClaw/OpenShell sandbox**:
inference is gateway-routed (`inference.local` → NVIDIA Nemotron 3, the raw key
never enters the sandbox), the Telegram token lives in the OpenShell gateway
(the sandbox sees only an `openshell:resolve:env:` placeholder, resolved at
egress), and the network policy is deny-by-default. The **ONE scheduler is the
OpenClaw cron inside the sandbox**: cron fires run `herr-claw daemon --trigger
<job`⟩ in the sandbox, which makes ONE audited `run_job` call to the host
bridge, where the job body executes (calendar/Reminder booking, vault recap,
Telegram send) against `./state/` — state stays host-only and never enters the
sandbox.

One-time sandbox setup (P0/P5 did this; commands for reproduction):

```sh
# prerequisites: Docker Desktop running + docker context use default
nemoclaw my-assistant start                       # start the sandbox (P0 onboarded it)
nemoclaw my-assistant policy add --from-file nemoclaw-blueprint/policy-additions.yaml --yes
#   ↑ then apply the increments nemoclaw's merge cannot carry (allowed_ips +
#     binaries pin) — see the header of policy-additions.yaml for the exact
#     `openshell policy set` recipe and verify with `nemoclaw my-assistant doctor`
uv run herr-claw bridge &                         # host bridge (with HERR_BRIDGE_ALLOWED_HOSTS=host.openshell.internal:8765)
uv run herr-claw daemon --install-cron            # register the 3 cron jobs from agent/schedule.yaml
```

The sandbox's trigger venv is vendored from the host (the sandbox never talks
to pypi): build the wheel (`uv build --wheel`), download linux-aarch64 wheels
for the core deps, `nemoclaw my-assistant upload` both, then
`uv pip install --no-index --find-links … herr-claw` into
`/sandbox/herrclaw/.venv`. `nemoclaw my-assistant doctor` verifies gateway,
policy and inference health.

## Usage

```sh
uv run herr-claw chat       # text chat in the terminal
uv run herr-claw bridge     # MCP bridge on 127.0.0.1:8765 (second terminal)
uv run herr-claw sprechen   # voice mode: mic → Whisper (de) → reply → Anna
uv run herr-claw daemon     # Telegram loop + break-glass job runner (host)
```

The scheduled 08:00 nudge / 12:30 quiz / 20:00 recap come from the OpenClaw
cron in the sandbox (see Deployment). `uv run herr-claw daemon` is the
interactive Telegram loop (quiz answers, slash commands) and the break-glass
runner for when the sandbox is down — it shares `session.json` dedup with the
cron, so a job can never fire twice in one day.

Run `herr-claw bridge` whenever the agent should book real Reminders/Calendar
entries, pick up the day's topic from your calendar, or write recaps.
Everything works without it — chat simply skips the booking, and the daemon
says so honestly. The daemon additionally needs `TELEGRAM_BOT_TOKEN` +
`HERR_TELEGRAM_CHAT_ID` (it has no reason to exist without the phone) and
`HERR_VAULT` for the recap.

In chat, talk to Herr Claw in German and use the commands:

| Command           | What it does                                                                                      |
| ----------------- | ------------------------------------------------------------------------------------------------- |
| `/üben <Thema>`   | Practice a topic (e.g. `/üben Bäckerei`) — with the bridge: books Reminder + 15-min Calendar slot |
| `/quiz <n>`       | Vocabulary quiz from the 100-word seed + your SRS state (default 5 questions, `„ende"` quits)     |
| `/fehler`         | Show your last mistakes with corrections                                                          |
| `/fortschritt`    | Streak, totals, SRS vocab, current topic/pause                                                    |
| `/erkläre <Wort>` | Explain a word in English, examples in German                                                     |
| `/pause <Tage>`   | Pause practice for a while (the daemon skips its jobs too)                                        |
| `/sprechen`       | Start voice mode (runs in the terminal: `herr-claw sprechen`)                                     |

Leave the chat with `Ctrl-D`; the session summary, streak, and any new
mistakes are persisted then. In the daemon, the same commands arrive as
Telegram messages and the replies land back in the chat.

## How it fits together

```
main.py                 CLI entry (`herr-claw` script): env load + Typer app
nemoclaw-blueprint/
  policy-additions.yaml OpenShell policy preset: bridge endpoint for the cron trigger (P5)
agent/
  SOUL.md               Tutor persona/system prompt (German, A1–A2, TTS-safe)
  schedule.yaml         THE scheduler config (the three job times, Berlin wall clock)
  config.py             Config from env; ONE state path: <repo>/state/ (host-only)
  chat.py               The read–reply TUI loop, history window, persistence
  commands.py           The stable German slash commands (dispatch → direct or LLM)
  quiz.py               SRS quiz engine — seed-constrained, 5 question types
  telegram.py           Bot API client (long poll, single-chat allowlist, no token in logs)
  cron_sync.py          P5: installs the OpenClaw cron jobs + the sandbox trigger client
  jobs.py               P5: host-side job execution engine (the run_job tool's body)
  daemon.py             Telegram loop + break-glass runner · 08:00 nudge · 12:30 quiz · 20:00 recap
  llm.py                Nemotron client, sanitization, correction parsing
  bridge.py             Sync MCP client to the bridge — the ONLY Apple/vault path for agent code
  scheduling.py         Free-slot picker, calendar-title → topic map, /üben booking flow
  state.py              srs.json + session.json (atomic, self-healing)
  tracker.py            Writes session summaries/mistakes to the vault
voice/
  stt.py                mlx-whisper base (de): mic capture, silence-stopped, MacBook mic picked
  tts.py                `say -v Anna` with markdown/emoji stripped, blocks until done
  loop.py               The sprechen push-to-talk loop (same state + commands as chat)
herrclaw_bridge/
  server.py             MCP Streamable-HTTP server on 127.0.0.1:8765 (7 tools, glue only)
  vault.py              Path-scoped, append-only Obsidian access (the allowlist IS the governance)
  reminders.py          Apple Reminders via osascript, pinned to the `Deutsch` list
  calendar_apple.py     EventKit freebusy/add (+ AppleScript fallback), pinned to `Deutsch Lernen`
  applescript.py        The single osascript gateway + AppleScript string escaping
  dates.py / errors.py  Local-time ISO parsing; BridgeError vs BridgeDenied
  audit.py              JSONL audit log for every governed call
seed/
  vocab_a1_100.csv      100 A1 words (article + plural, themes, cloze examples)
state/                  Runtime state (gitignored): srs.json, session.json
tests/                  pytest suite for all of the above (251 tests)
```

A chat turn flows: input → command dispatch (direct answer, or message plus
optional system note — `/üben` books through the bridge here) → Nemotron
reply → markdown/reasoning sanitization → correction extraction → state
update; vault writes happen when corrections are logged and when the session
ends. The voice loop and the daemon reuse exactly those pieces — same state
path, same quiz engine, same `dispatch()` — so what you practice in the
terminal, speak into the mic, and answer on the phone is one continuous
learning state.

## Safety and governance

- **One state path.** All persistent tutor state lives in `state/`; there is
  no second scheduling or storage mechanism.
- **Apple access only through the bridge.** Agent code never calls
  `osascript` or EventKit — it goes through the MCP bridge on
  `127.0.0.1:8765`, where allowlist enforcement and audit live. The bridge
  binds to loopback only.
- **Scoped to one list, one calendar, three folders.** Reminders are pinned
  to the `Deutsch` list, calendar writes to the `Deutsch Lernen` calendar
  (the MCP tool exposes no calendar parameter at all — a foreign calendar is
  not merely denied, it is unexpressable), and vault access to
  `Deutsch/`, `Daily notes/`, and `Weeks/` inside `HERR_VAULT`. Free/busy
  reads all calendars (read-only) so the slot picker sees your real day.
- **Vault allowlist.** Writes go through `vault.append` only — there is no
  edit or delete API — and path traversal, separator escapes, and anything
  outside the allowlist raise `VaultDenied`.
- **Audit trail.** Every vault, Reminders, and Calendar call — allowed or
  denied — is one JSON line in `~/.herr-claw/audit.log` with timestamp,
  action, target, and denial reason. That log is the demo evidence that the
  agent stays in its lanes.
- **Secrets stay in the environment.** `NVIDIA_API_KEY` and
  `TELEGRAM_BOT_TOKEN` are read from the environment only (`.env` in
  host-fallback mode); they are never hardcoded, logged, or written into
  agent-readable state. The bridge itself needs no secrets, and the
  Telegram bot answers exactly one chat id — everyone else is ignored.
- **Network surface.** The tutor talks only to `integrate.api.nvidia.com`
  and — in daemon mode — to `api.telegram.org`; the bridge is local-only
  on `127.0.0.1:8765`.
- **Quiz content is constrained.** Questions are generated only from the
  seed CSV and words already in the SRS — quiz vocabulary is data, never
  model output, so the tutor cannot invent words you never learned.

## Tests

```sh
uv run pytest
```

251 tests (plus one opt-in live STT test:
`HERR_LIVE_STT=1 uv run pytest tests/test_stt.py -k live`). The suite
covers the chat loop, command dispatch (including `/üben` booking against a
fake bridge and the real `/quiz` flow with SRS updates), the quiz engine
(seed integrity, question types, lenient answer matching, weakest-first
selection), the Telegram client (retries, chunking, 409 conflicts, and
token hygiene — error messages must never contain it), the daemon (schedule
loading, job dedup and catch-up, all three §4.1 jobs against fake
bridge/Telegram, graceful Ctrl-C), correction parsing and sanitization,
SRS/streak/session state, the CLI, the vault allowlist (including denial
paths), the slot picker and topic map as pure functions, AppleScript
generation and escaping, the EventKit components against a fake
pyobjc-shaped EventKit (including the AppleScript fallback and denied
foreign calendars), and the MCP bridge end-to-end: a real uvicorn server +
real MCP client over HTTP with the Apple backends faked at module level —
no test ever touches the real vault, Reminders, Calendar, Telegram, or
`~/.herr-claw`.

## Roadmap

1. **Overnight evidence**: let the OpenClaw-cron scheduler run for real
   nights so the 08:00 nudge, booking, and 20:00 recap artifacts accumulate
   on their own (the cron entries are installed and live).
2. **v2 ideas**: Nemotron omni audio-in for the voice loop once German
   accuracy suffices, the Sunday weekly review, pronunciation scoring,
   moving the interactive Telegram channel into the OpenClaw runtime
   (the gateway already holds the token; today the host daemon keeps the
   single `getUpdates` poll for the interactive quiz).
