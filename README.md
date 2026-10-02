# Herr Claw 🇩🇪

A long-running, sandboxed **German-tutor agent** for the NVIDIA Claw Agent Challenge —
for anyone at A1–A2 who lives in Apple Calendar, Reminders and Obsidian and
wants a daily five-minute German habit. Herr Claw chats with you in simple
German (text or voice), corrects your mistakes gently, quizzes you with
spaced repetition, and logs your progress to Obsidian.

It is a **long-running agent — and the agent genuinely lives in the sandbox**:
the OpenClaw runtime persists in its NemoClaw/OpenShell sandbox, the cron
fires the day's jobs every day — morning, midday, evening — unattended, and
since P6 the brain itself (every conversation turn, the job bodies, the
Telegram receiver, all state) executes inside that sandbox, while each
execution stays a short one-shot and the host runs only thin frontends plus
the audited Apple/vault bridge. In the morning it reads your calendar and
hands you German phrases tailored to what you actually have on today; at
night you tell it which phrases you used and it notes them — along with your
interests — in its memory, so every lesson and reply keeps fitting you.
Midday micro-quiz, evening recap, and the nudges land on Telegram — and the
phone is a first-class conversation channel: you answer, and the same brain
that runs in your terminal replies.

Built for macOS (Apple Silicon only, by design), Python 3.14, managed with
[`uv`](https://docs.astral.sh/uv/). Inference runs on
**NVIDIA Nemotron 3** (`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`)
via the OpenAI-compatible endpoint at `integrate.api.nvidia.com`.

### Why a spare Mac and not the cloud

The deployment target here is deliberately the one most people already own:
**a Mac mini or an old MacBook on a shelf, running for free** — no rented
cloud VM, no monthly bill. The OpenClaw sandbox, the audited bridge into
Apple Calendar/Reminders/Obsidian, and the day's cron jobs all run on
hardware you already have, fenced by OpenShell policy instead of a cloud
security group.

Full disclosure: I'm on a career break, so the infra budget is exactly
zero euros — which turns out to be a feature; it forces the architecture to
be honest about what "always-on" really means. It does mean, though, that
Herr Claw currently thinks on borrowed GPU cycles in NVIDIA's cloud. A
certain desk-sized DGX Spark would change that: the same sandbox, the same
bridge, the same cron — but the tutor's brain running locally and silently,
fully off-grid. If you're weighing who would actually use one as the
always-on home for a sandboxed agent, this submission is making that case. 😉

## Quickstart

```sh
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash   # 0. one-time: install NemoClaw + OpenShell from NVIDIA
nemoclaw onboard                                       #    one-time: creates the gateway + the my-assistant sandbox
uv sync --all-extras && cp .env.example .env           # 1. fill in the values
nemoclaw my-assistant status                           # 2. the sandbox must be RUNNING — chat is a thin client to it
#   stopped → `nemoclaw my-assistant start` (needs Docker Desktop up)
#   no sandbox yet → nemoclaw-blueprint/build-deployment.sh (builds the whole always-on deployment)
uv run herr-claw chat                                  # 3. talk to it right now — the sandbox brain replies
```

NemoClaw is NVIDIA's own agent-runtime CLI — it has to be downloaded from
NVIDIA first (the installer above brings the `nemoclaw` + `openshell` CLIs,
the OpenClaw runtime, and Docker Desktop as its dependency); `nemoclaw
onboard` then creates the gateway and the sandbox once. Details:
[docs.nvidia.com/nemoclaw](https://docs.nvidia.com/nemoclaw).

> Chat is a thin client: the brain runs in the sandbox. If the sandbox is
> down, the chat still starts — it then falls back to the local break-glass
> brain on the host (not the deployment this README describes). Always run
> the `status` check above first; `nemoclaw my-assistant doctor` verifies
> gateway, policy and inference health.

The full build book — problem statement, architecture, what every step does,
real example output, verification, troubleshooting — lives in
[nemoclaw-blueprint/DEPLOYMENT.md](nemoclaw-blueprint/DEPLOYMENT.md).
The repeatable setup → test → shutdown runbook (same content as this
Quickstart plus a prompted check after every function, down to the phone
end-to-end test) is [script.md](script.md).

## Status — what works today

- **Text chat TUI** (`herr-claw chat`): on a real terminal, the moustache
  mascot (`:-{)` marks every agent turn) sits in a fixed banner with the
  transcript scrolling beneath it and a status strip (streak, due cards,
  topic) pinned to the last row; off-TTY it degrades to the plain German
  tutor loop. Since P6 the TUI is a thin client — every user line is one
  `nemoclaw exec` into the sandbox brain (`herr-claw turn`); if the sandbox
  is unreachable it falls back to a local break-glass brain with one warning
  — so a working chat does **not** prove the sandbox is up: check
  `nemoclaw my-assistant status` first.
  Persona (`agent/SOUL.md`), rolling history, and soft corrections in the
  fixed template `Richtig: … / Deine Version: …` live with the brain.
- **Voice mode** (`herr-claw sprechen`): push-to-talk (space/enter) →
  mlx-whisper `base` (language locked to `de`, 16 kHz mono, silence-stopped
  ≤5 s) → Nemotron reply → `say -v Anna` (markdown and emoji stripped before
  speaking). The MacBook mic is picked explicitly — iPhone Continuity ghost
  mics are skipped — and the per-turn latency is printed live against the
  <5 s budget. Typed commands still work next to the mic; Ctrl-C quits and
  saves state.
- **German slash commands**: `/üben`, `/quiz`, `/fehler`, `/erfolge`, `/tag`,
  `/fortschritt`, `/erkläre`, `/pause`, `/sprechen` — identical behavior in
  the TUI, voice mode, and Telegram because all three go through one
  `dispatch()`. `/erkläre`, `/üben` and `/erfolge` steer the tutor via
  system notes; the rest are answered directly from local state.
- **Day memory** (`memory.md` in the agent's state dir): the agent's own
  journal — profile, which topics landed vs flopped, phrases you report
  using successfully (`/erfolge <Satz>`, or just tell it in the evening
  check-in). A reported phrase reinforces the matching SRS words, lands in
  the journal, and is appended to `Deutsch/progress.md`; the journal
  tailors every chat, voice, and morning-brief prompt (phrasing and topics
  only — never quiz content).
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
- **The day loop** is the OpenClaw cron inside the sandbox — the ONE
  scheduler, configured only by `agent/schedule.yaml` — and since P6 the
  job bodies execute IN the sandbox too: **08:00** Morgen-Brief — read the
  calendar (via the audited bridge), derive the day's topic (calendar
  keyword first, else the weakest vocab's theme), tailor 5–8 German phrases
  to the actual day (event themes, due SRS vocab woven in via the seed's
  cloze examples, memory interests for flavor — real events only, an honest
  note when the calendar was unreadable), book a free 15-minute slot
  (Reminder + Calendar event via the bridge), and push the brief to
  Telegram + the daily note (`/tag` re-shows it all day); **12:30**
  micro-quiz — 3 questions from your weakest vocab, only if the
  Morgen-Brief ran, answered right on the phone (the quiz persists in
  `session.json`, the Telegram receiver grades it); **20:00** recap —
  append the daily note + `Deutsch/progress.md` to Obsidian (through the
  bridge), mark the practice reminder complete, and ask on Telegram which
  phrases you actually used (Erfolgs-Check).
  Job dedup lives in `session.json` (a re-fire never double-runs a job),
  jobs missed earlier today catch up oldest-first on the next fire,
  `/pause` is respected, and a failed job retries without being marked
  done.
- **Telegram** is a full conversation channel: `herr-claw telegram-loop`
  runs IN the sandbox, long-polls the Bot API (`api.telegram.org` only,
  stdlib client, gateway-resolved token) and answers every message through
  the same brain as the TUI and voice. It answers exactly ONE chat
  (`HERR_TELEGRAM_CHAT_ID`); other chats are logged and ignored, never
  answered. A `*/5` watchdog cron entry (`telegram-watchdog`) restarts the
  loop if it ever dies — the ONE receiver stays up.
- **MCP bridge** (`herr-claw bridge`): exposes seven tools over
  Streamable-HTTP MCP on `127.0.0.1:8765` — `reminders.{list,add,complete}`
  (AppleScript, pinned to the `Deutsch` list), `calendar.{freebusy,add}`
  (EventKit, writes pinned to the `Deutsch Lernen` calendar, AppleScript
  fallback only if EventKit is unusable), and `vault.{read,append}`. It is
  the ONE Apple/vault door: agent code never touches `osascript` or
  EventKit directly — neither host-side nor in the sandbox (vault writes
  from the sandbox go through the same tools via the `BridgeVault`
  adapter) — so allowlist enforcement and audit live in exactly one place.
  The bridge needs no secrets, only `HERR_VAULT`.
- **Learning state** in the ONE state path (`HERR_STATE_DIR`; inside the
  sandbox: `/sandbox/herrclaw/state`): mistake log (deduplicated with
  counts), daily streak, session/message/correction totals, and the
  SM-2-lite spaced repetition engine (L1→1d, L2→3d, L3→7d, L4→14d, L5→30d;
  a miss drops one level). State files are written atomically; a corrupt
  `srs.json` is backed up, never silently dropped. `session.json` also
  carries the cross-process continuity: the rolling history window, the
  active quiz, and the open-session counters.
- **Obsidian logging** through the bridge's scoped vault: append-only writes
  to `Deutsch/progress.md`, `Deutsch/fehler.md`, and
  `Daily notes/YYYY-MM-DD-deutsch.md`. If the bridge/vault is unavailable,
  the brain still runs and only its own state persists.
- **Audit trail**: every vault, Reminders, and Calendar call — allowed _and_
  denied — lands as one JSON line in `~/.herr-claw/audit.log`.
- **Robust inference**: `thinking` disabled on every call (reasoning mode
  measured 9–54 s vs ~1.4 s without), retry with backoff on transient
  free-tier 503s, friendly German error messages, and reply sanitization
  that strips reasoning blocks and markdown so answers stay short and
  speakable (they will be read aloud).

Not yet: the Sunday weekly review and the demo video. The sandbox-in
deployment (OpenClaw cron scheduler, OpenShell policy with the bridge
endpoint + caller pin, vendored venv) is live and verified end to end from
the sandbox (2026-10-02, see [script.md](script.md) T1–T7): sandbox brain
turns (direct + Nemotron via gateway-routed inference), bridge reads and
writes through the audited bridge, all four cron jobs, the polling Telegram
receiver, and a real `trigger nudge` (Reminder + daily note + Telegram
push). Two prerequisites this surfaced: the `Deutsch Lernen` calendar must
already exist in Calendar.app (the account forbids programmatic calendar
creation), and cron entries must carry the full gateway-issued Telegram
token placeholder (see `TELEGRAM_TOKEN_PLACEHOLDER` in `agent/cron_sync.py`).
The sandbox agent can call the bridge tools as MCP (`policy v11`), the brain
and the Telegram receiver run in the sandbox, and the day is self-healing:
a trigger retries a briefly-down bridge, every fire first catches up
earlier jobs that failed earlier in the day (a lost 08:00 nudge runs when
the 12:30 quiz fire succeeds), and the watchdog brings a dead Telegram
receiver back within five minutes.

## Requirements

- macOS on Apple Silicon (uses `say`, Apple Reminders/Calendar, mlx-whisper)
- Python 3.14+ and [`uv`](https://docs.astral.sh/uv/)
- An NVIDIA Build API key (https://build.nvidia.com)
- Optional: a microphone for voice mode, an Obsidian vault for progress
  notes, and a Telegram bot token + your chat id for the daemon's nudges
- For the bridge: macOS Automation permission for Reminders + Calendar, and
  EventKit calendar access (first call per context triggers the prompt once)
- For the sandbox-in deployment: [NemoClaw](https://docs.nvidia.com/nemoclaw)
  with the OpenClaw runtime — download it from NVIDIA first
  (`curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash`; it installs Docker
  Desktop as its own dependency), then `nemoclaw onboard` sets up the
  gateway + sandbox

## Setup

```sh
uv sync --all-extras    # create venv and install dependencies (incl. voice + bridge extras)
cp .env.example .env    # then fill in the values
```

Environment variables (documented in `.env.example`; `.env` is gitignored
and holds live secrets — never commit it):

| Variable                | Purpose                                                                                               |
| ----------------------- | ----------------------------------------------------------------------------------------------------- |
| `NVIDIA_API_KEY`        | NVIDIA Build API key for Nemotron inference (host dev/break-glass; the sandbox uses gateway-routed inference with no key value) |
| `TELEGRAM_BOT_TOKEN`    | Telegram bot token (gateway-held in the deployment; host `.env` is the dev/break-glass fallback)      |
| `HERR_TELEGRAM_CHAT_ID` | The single allowed Telegram chat — the only one the bot answers                                       |
| `HERR_VAULT`            | Path to your Obsidian vault (host-only); unset disables vault tools/writes                            |
| `HERR_BRIDGE_URL`       | MCP bridge endpoint; empty → `http://127.0.0.1:8765/mcp`, `off` disables bridge calls                 |
| `HERR_BRIDGE_ALLOWED_HOSTS` / `HERR_BRIDGE_ALLOWED_ORIGINS` | Extra Host/Origin values the bridge accepts under a non-localhost name (sandbox-in deployment; see below) |
| `HERR_NEMOCLAW_SANDBOX` | NemoClaw sandbox the agent lives in (default `my-assistant`)                                          |
| `HERR_STATE_DIR`        | State directory override; default `<repo>/state` on the host, `/sandbox/herrclaw/state` in the sandbox |
| `HERR_CHAT_BACKEND`     | Frontend brain: `sandbox` (default) or `local` (break-glass; AutoBrain also falls back automatically) |
| `HERR_MODEL`            | Optional model override                                                                               |
| `HERR_NVIDIA_BASE_URL`  | Optional endpoint override (`https://inference.local/v1` inside the sandbox)                          |
| `HERR_WHISPER_MODEL`    | Whisper model for voice mode (default `base` — never `tiny`, too weak for German)                     |
| `HERR_MIC_DEVICE`       | Optional input device override (name substring or index); auto-pick skips iPhone/iPad Continuity mics |

## Deployment — OpenClaw via NemoClaw/OpenShell (the primary mode)

Herr Claw deploys as an **OpenClaw agent inside a NemoClaw/OpenShell sandbox** —
and since P6 the agent's brain lives there: conversation turns, job bodies,
Telegram receiver, and all state execute in the sandbox. Inference is
gateway-routed (`inference.local` → NVIDIA Nemotron 3, the raw key never
enters the sandbox), the Telegram token lives in the OpenShell gateway (the
sandbox sees only an `openshell:resolve:env:` placeholder, resolved at
egress), and the network policy is deny-by-default. The **ONE scheduler is
the OpenClaw cron inside the sandbox**: cron fires run `herr-claw trigger
<job>` in the sandbox, where the job body executes against
`/sandbox/herrclaw/state` — only its Apple/vault calls cross the audited
host bridge. The host runs the bridge and the thin frontends; it holds no
agent logic and no agent state.

**The box — how the claw is fenced off from the Apple tools, your vault,
and the secrets:**

```
┌────────────────────────────────────────────────────────────────────────────┐
│macOS host — thin frontends + the ONE capability door                       │
│                                                                            │
│  herrclaw_bridge (MCP server, 127.0.0.1:8765 — 7 audited tools)            │
│    reminders.*  → Apple Reminders (list `Deutsch`)                         │
│    calendar.*   → Apple Calendar (`Deutsch Lernen`)                        │
│    vault.*      → Obsidian vault (3 scoped folders)                        │
│  TUI `herr-claw chat` / voice `sprechen`: rendering + macOS audio only —   │
│    each user message is ONE `nemoclaw exec … herr-claw turn`               │
│                                                                            │
│  audit: ~/.herr-claw/audit.log — every allow AND deny, one JSON line       │
│  secrets: NVIDIA_API_KEY + TELEGRAM_BOT_TOKEN — gateway-held (host         │
│           .env is dev/break-glass fallback only); never inside the box     │
│  OpenShell gateway: routes egress, resolves inference + telegram tokens    │
└────────────▲───────────────────────────────────────────────────────────────┘
             │ Apple/vault calls only, audited at the bridge
             │ host.openshell.internal:8765
             │ /mcp verbs only · allowed_ips · binaries pin (trigger venv
             │   + curl + the OpenClaw runtime — policy v11)
┌────────────────────────────────────────────────────────────────────────────┐
│OpenShell sandbox `my-assistant` (Docker) — THE AGENT LIVES IN THIS BOX     │
│                                                                            │
│  OpenClaw agent runtime (dashboard 127.0.0.1:18789)                        │
│    OpenClaw cron = the ONE scheduler, from agent/schedule.yaml             │
│      fire: `herr-claw trigger <job>` → job body executes HERE              │
│      watchdog: `telegram-watchdog` every 5 min (self-healing receiver)     │
│    `herr-claw turn`: the brain — one process per TUI/voice/Telegram        │
│          message (vendored venv; the sandbox never talks to pypi)          │
│    telegram-loop: long-poll getUpdates → same brain → sendMessage          │
│                                                                            │
│  state: /sandbox/herrclaw/state (srs.json · session.json · memory.md)      │
│  filesystem: /sandbox + /tmp writable — the vault only via the bridge      │
│                                                                            │
│  egress deny-by-default — exactly 3 doors out:                             │
│    Apple/vault ──────► the MCP bridge above (tools + audit)                │
│    inference.local ───► OpenShell gateway → NVIDIA Nemotron 3              │
│    api.telegram.org ──► OpenShell gateway (token resolved at egress)       │
│                                                                            │
│  everything else: DENIED (OCSF policy log = the on-screen evidence)        │
└────────────────────────────────────────────────────────────────────────────┘
```

One-time sandbox setup (P0/P5 did this; commands for reproduction):

```sh
# prerequisites: Docker Desktop running + docker context use default
nemoclaw my-assistant start                       # start the sandbox (P0 onboarded it)
nemoclaw my-assistant policy add --from-file nemoclaw-blueprint/policy-additions.yaml --yes
#   ↑ then apply the increments nemoclaw's merge cannot carry (allowed_ips +
#     binaries pin) — see the header of policy-additions.yaml for the exact
#     `openshell policy set` recipe and verify with `nemoclaw my-assistant doctor`
uv run herr-claw bridge &                         # host bridge (with HERR_BRIDGE_ALLOWED_HOSTS=host.openshell.internal:8765)
uv run herr-claw install-cron                     # register the 3 cron jobs + telegram watchdog from agent/schedule.yaml
```

The sandbox's trigger venv is vendored from the host (the sandbox never talks
to pypi): build the wheel (`uv build --wheel`), download linux-aarch64 wheels
for the core deps, `nemoclaw my-assistant upload` both, then
`uv pip install --no-index --find-links … herr-claw` into
`/sandbox/herrclaw/.venv`. `nemoclaw my-assistant doctor` verifies gateway,
policy and inference health.

## Usage

```sh
nemoclaw my-assistant status      # FIRST: is the sandbox up? (if stopped: nemoclaw my-assistant start)
uv run herr-claw chat             # text chat in the terminal (thin client → sandbox brain)
uv run herr-claw bridge           # MCP bridge on 127.0.0.1:8765 (the Apple/vault door)
uv run herr-claw sprechen         # voice mode: mic → Whisper (de) → sandbox brain → Anna
uv run herr-claw install-cron     # register the cron jobs + watchdog in the sandbox
```

The sandbox check comes first on purpose: the frontends are thin clients,
so `chat` and `sprechen` are only talking to the real brain while
`my-assistant` is running. With the sandbox down they still start — the
AutoBrain silently switches to the local break-glass brain on the host
(turns and state then land on the host, not in `/sandbox/herrclaw/state`).
That mode is for break-glass only; before testing anything, make sure
`nemoclaw my-assistant status` is green (`nemoclaw my-assistant doctor`
for the full gateway/policy/inference check).

The scheduled 08:00 nudge / 12:30 quiz / 20:00 recap come from the OpenClaw
cron in the sandbox (see Deployment); the Telegram conversation receiver
(`telegram-loop`) and its watchdog run there too. In the deployment, every
piece above lives where it belongs — the frontends on the Mac, the agent in
the sandbox.

Run `herr-claw bridge` whenever the agent should book real Reminders/Calendar
entries, pick up the day's topic from your calendar, or write recaps.
Everything works without it — chat simply skips the booking, and the brain
says so honestly.

In chat, talk to Herr Claw in German and use the commands:

| Command             | What it does                                                                                      |
| ------------------- | ------------------------------------------------------------------------------------------------- |
| `/üben <Thema>`     | Practice a topic (e.g. `/üben Bäckerei`) — with the bridge: books Reminder + 15-min Calendar slot |
| `/quiz <n>`         | Vocabulary quiz from the 100-word seed + your SRS state (default 5 questions, `„ende"` quits)     |
| `/tag`              | Today's tailored phrase plan — re-shows (or lazily builds) the Morgen-Brief                       |
| `/erfolge <Satz>`   | Report a phrase you really used — gentle correction if needed, remembered as a success            |
| `/fehler`           | Show your last mistakes with corrections                                                          |
| `/fortschritt`      | Streak, totals, SRS vocab, current topic/pause                                                    |
| `/erkläre <Wort>`   | Explain a word in English, examples in German                                                     |
| `/pause <Tage>`     | Pause practice for a while (the day loop skips its jobs too)                                      |
| `/sprechen`         | Start voice mode (runs in the terminal: `herr-claw sprechen`)                                     |

Leave the chat with `Ctrl-D`; the brain persists the session summary, streak,
and any new mistakes (it closes the session after 60 idle minutes at the
latest). The same commands arrive as Telegram messages and the replies land
back in the chat — the phone talks to the same brain as your terminal.

## How it fits together

```
main.py                 CLI entry (`herr-claw` script): env load + Typer app
nemoclaw-blueprint/
  policy-additions.yaml OpenShell policy preset: bridge endpoint for the sandboxed agent (P5)
agent/
  SOUL.md               Tutor persona/system prompt (German, A1–A2, TTS-safe)
  schedule.yaml         THE scheduler config (3 job times + watchdogs; Berlin wall clock / cron exprs)
  config.py             Config from env; ONE state path via HERR_STATE_DIR (sandbox: /sandbox/herrclaw/state)
  turns.py              P6: THE BRAIN — LocalBrain (`herr-claw turn`, runs in the sandbox; break-glass
                        on the host), SandboxBrain (thin client over `nemoclaw exec`), AutoBrain fallback
  chat.py               The chat frontend: moustache TUI (banner, :-{) turns, status line) — thin client
  commands.py           The stable German slash commands (dispatch → direct or LLM)
  memory.py             memory.md journal — profile, interests, successes (v0.5)
  phrases.py            Morgen-Brief builder: event themes + due vocab + memory → 5–8 phrases
  quiz.py               SRS quiz engine — seed-constrained, 5 question types, persists across processes
  telegram.py           Bot API client (sends + getUpdates long poll, single-chat allowlist, no token in logs)
  poller.py             P6: telegram-loop (THE conversation receiver, in-sandbox) + telegram-watchdog
  cron_sync.py          P5/P6: installs the OpenClaw cron jobs (+watchdogs) + the in-sandbox trigger path
  jobs.py               P5/P6: job engine (nudge/quiz/recap, dedup, catch-up) — bodies run IN the sandbox
  llm.py                Nemotron client, sanitization, correction parsing
  bridge.py             Sync MCP client + BridgeVault — the ONLY Apple/vault path for agent code
  scheduling.py         Free-slot picker, calendar-title → topic map, /üben booking flow
  state.py              srs.json + session.json (atomic, self-healing; history/quiz/session counters)
  tracker.py            Writes session summaries/mistakes to the vault
voice/
  stt.py                mlx-whisper base (de): mic capture, silence-stopped, MacBook mic picked
  tts.py                `say -v Anna` with markdown/emoji stripped, blocks until done
  loop.py               The sprechen push-to-talk loop (thin client → sandbox brain)
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
state/                  Host default of HERR_STATE_DIR (gitignored) — the live path is /sandbox/herrclaw/state
tests/                  pytest suite for all of the above
```

A chat turn flows: input → one `nemoclaw exec` into the sandbox → command
dispatch (direct answer, or message plus optional system note — `/üben`
books through the bridge) → Nemotron reply (gateway-routed inference) →
markdown/reasoning sanitization → correction extraction → state update in
`/sandbox/herrclaw/state`; vault writes cross the bridge when corrections
are logged and when the session closes (60 idle minutes at the latest). The
voice loop and the Telegram receiver reuse exactly those pieces — same
brain seam, same state path, same quiz engine, same `dispatch()` — so what
you practice in the terminal, speak into the mic, and answer on the phone
is one continuous learning state.

## Safety and governance

- **One state path, with the agent.** All persistent tutor state lives in
  `HERR_STATE_DIR` — inside the sandbox in the deployment; there is no
  second scheduling or storage mechanism, and the host holds no agent state.
- **Apple access only through the bridge.** Agent code never calls
  `osascript` or EventKit — it goes through the MCP bridge on
  `127.0.0.1:8765` (in-sandbox: `host.openshell.internal:8765`), where
  allowlist enforcement and audit live. The bridge binds to loopback only.
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
- **Network surface.** The sandbox reaches exactly three doors — the audited
  bridge, gateway-routed inference (`inference.local`), and
  `api.telegram.org` (token resolved at egress); everything else is denied
  by OpenShell policy. The bridge itself is local-only on `127.0.0.1:8765`.
- **Quiz content is constrained.** Questions are generated only from the
  seed CSV and words already in the SRS — quiz vocabulary is data, never
  model output, so the tutor cannot invent words you never learned.

## Tests

```sh
uv run pytest
```

331 tests (plus one opt-in live STT test:
`HERR_LIVE_STT=1 uv run pytest tests/test_stt.py -k live`). The suite
covers the brain seam end to end (LocalBrain one-shot turns: history
continuing across processes, persisted quizzes graded by a later process,
the 60-minute session-gap close, calendar topic flourish, break-glass
degradation; SandboxBrain JSON transport; AutoBrain's one warned fallback;
the `turn` CLI), the chat loop and the TUI frame (moustache banner, `:-{)`
turns, status line, off-TTY fallback), command dispatch (including `/üben`
booking against a fake bridge, the real `/quiz` flow with SRS updates, and
the v0.5 `/tag` + `/erfolge` commands), the memory journal (atomic save,
self-healing load, prompt block), the Morgen-Brief builder (event themes,
due-vocab weaving, honest fallback), the quiz engine
(seed integrity, question types, lenient answer matching, weakest-first
selection), the Telegram client (retries, chunking, 409 conflicts, and
token hygiene — error messages must never contain it), the in-sandbox day
loop (schedule loading, job dedup and catch-up, all three §4.1 jobs against
fake bridge/Telegram, the evening Erfolgs-Check recording into memory +
SRS) plus the Telegram poller (single-chat fence, offset advance, backoff)
and its watchdog (idempotent restart, stale pidfiles), correction parsing
and sanitization, SRS/streak/session state, the CLI, the vault allowlist
(including denial paths), the slot picker and topic map as pure functions,
AppleScript generation and escaping, the EventKit components against a fake
pyobjc-shaped EventKit (including the AppleScript fallback and denied
foreign calendars), and the MCP bridge end-to-end: a real uvicorn server +
real MCP client over HTTP with the Apple backends faked at module level —
no test ever touches the real vault, Reminders, Calendar, Telegram, or
`~/.herr-claw`.

## Roadmap

1. **Overnight evidence**: let the OpenClaw-cron scheduler run for real
   nights so the 08:00 nudge, booking, and 20:00 recap artifacts accumulate
   on their own (the cron entries are installed and live; the sandbox path
   is verified end to end — see Status above).
2. **v2 ideas**: Nemotron omni audio-in for the voice loop once German
   accuracy suffices, the Sunday weekly review, pronunciation scoring,
   export/backup of the sandbox state (`herr-claw export-state`) so a full
   sandbox rebuild can restore SRS history from the host.
