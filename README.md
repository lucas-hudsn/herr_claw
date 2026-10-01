# Herr Claw 🤖

A long-running, sandboxed **German-tutor agent** for the NVIDIA Claw Agent
Challenge. Herr Claw chats with you in simple German (A1–A2), corrects your
mistakes gently, remembers your progress, and logs sessions to your Obsidian
vault — designed to schedule practice around your life (Apple
Calendar/Reminders), run spaced repetition, and eventually talk and listen.

Built for macOS (Apple Silicon only, by design), Python 3.14, managed with
[`uv`](https://docs.astral.sh/uv/). Inference runs on
**NVIDIA Nemotron 3** (`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`)
via the OpenAI-compatible endpoint at `integrate.api.nvidia.com`.

## Status — what works today

- **Text chat TUI** (`herr-claw chat`): a German-only tutor loop with a
  persona prompt (`agent/SOUL.md`), rolling history, and soft corrections in
  the fixed template `Richtig: … / Deine Version: …`.
- **German slash commands**: `/üben`, `/quiz`, `/fehler`, `/fortschritt`,
  `/erkläre`, `/pause`, `/sprechen`. `/erkläre` and `/üben` steer the tutor
  via system notes; `/fortschritt`, `/fehler`, `/pause` are answered
  directly from local state; `/quiz` and `/sprechen` honestly say they are
  not here yet.
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
- **MCP bridge** (`herr-claw bridge`): exposes seven tools over
  Streamable-HTTP MCP on `127.0.0.1:8765` — `reminders.{list,add,complete}`
  (AppleScript, pinned to the `Deutsch` list), `calendar.{freebusy,add}`
  (EventKit, writes pinned to the `Deutsch Lernen` calendar, AppleScript
  fallback only if EventKit is unusable), and `vault.{read,append}`. Agent
  code never touches `osascript` or EventKit directly — the bridge client
  (`agent/bridge.py`) is the only path, so allowlist enforcement and audit
  live in exactly one place. The bridge needs no secrets, only `HERR_VAULT`.
- **Learning state** in `state/`: mistake log (deduplicated with counts),
  daily streak, session/message/correction totals, and an SM-2-lite spaced
  repetition engine (L1→1d, L2→3d, L3→7d, L4→14d, L5→30d; a miss drops one
  level). Both `state/srs.json` and `state/session.json` are written
  atomically; a corrupt `srs.json` is backed up, never silently dropped.
- **Obsidian logging** through the bridge's scoped vault: append-only writes
  to `Deutsch/progress.md`, `Deutsch/fehler.md`, and
  `Daily notes/YYYY-MM-DD-deutsch.md`. If `HERR_VAULT` is unset, chat still
  works and only `state/` persists.
- **Audit trail**: every vault, Reminders, and Calendar call — allowed *and*
  denied — lands as one JSON line in `~/.herr-claw/audit.log`.
- **Robust inference**: `thinking` disabled on every call (reasoning mode
  measured 9–54 s vs ~1.4 s without), retry with backoff on transient
  free-tier 503s, friendly German error messages, and reply sanitization
  that strips reasoning blocks and markdown so answers stay short and
  speakable (they will be read aloud).

Not yet: voice mode (next — STT/TTS deps are already in), vocabulary quiz,
Telegram, sandbox policy wiring.

## Requirements

- macOS on Apple Silicon (uses `say`, Apple Reminders/Calendar, mlx-whisper)
- Python 3.14+ and [`uv`](https://docs.astral.sh/uv/)
- An NVIDIA Build API key (https://build.nvidia.com)
- Optional: an Obsidian vault for progress notes
- For the bridge: macOS Automation permission for Reminders + Calendar, and
  EventKit calendar access (first call per context triggers the prompt once)

## Setup

```sh
uv sync                 # create venv and install dependencies
cp .env.example .env    # then fill in the values
```

Environment variables (documented in `.env.example`; `.env` is gitignored
and holds live secrets — never commit it):

| Variable | Purpose |
| --- | --- |
| `NVIDIA_API_KEY` | NVIDIA Build API key for Nemotron inference (required) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (used once the Telegram bot lands) |
| `HERR_TELEGRAM_CHAT_ID` | The single allowed Telegram chat |
| `HERR_VAULT` | Path to your Obsidian vault; unset disables vault tools/writes |
| `HERR_BRIDGE_URL` | MCP bridge endpoint; empty → `http://127.0.0.1:8765/mcp`, `off` disables bridge calls |
| `HERR_MODEL` | Optional model override |
| `HERR_NVIDIA_BASE_URL` | Optional endpoint override |
| `HERR_WHISPER_MODEL` | Whisper model for voice mode (default `base`) |

## Usage

```sh
uv run herr-claw            # help
uv run herr-claw chat       # text chat in the terminal
uv run herr-claw bridge     # MCP bridge on 127.0.0.1:8765 (second terminal)
```

Run `herr-claw bridge` in a second terminal when you want `/üben` to book
real Reminders/Calendar entries and chat to pick up the day's topic from
your calendar. Everything works without it — chat simply skips the booking.

In chat, talk to Herr Claw in German and use the commands:

| Command | What it does |
| --- | --- |
| `/üben <Thema>` | Practice a topic (e.g. `/üben Bäckerei`) — with the bridge: books Reminder + 15-min Calendar slot |
| `/quiz` | Vocabulary quiz — coming soon |
| `/fehler` | Show your last mistakes with corrections |
| `/fortschritt` | Streak, totals, SRS vocab, current topic/pause |
| `/erkläre <Wort>` | Explain a word in English, examples in German |
| `/pause <Tage>` | Pause practice for a while |
| `/sprechen` | Voice mode — coming soon |

Leave the chat with `Ctrl-D`; the session summary, streak, and any new
mistakes are persisted then.

## How it fits together

```
main.py                 CLI entry (`herr-claw` script): env load + Typer app
agent/
  SOUL.md               Tutor persona/system prompt (German, A1–A2, TTS-safe)
  config.py             Config from env; ONE state path: <repo>/state/
  chat.py               The read–reply TUI loop, history window, persistence
  commands.py           The stable German slash commands (dispatch → direct or LLM)
  llm.py                Nemotron client, sanitization, correction parsing
  bridge.py             Sync MCP client to the bridge — the ONLY Apple/vault path for agent code
  scheduling.py         Free-slot picker, calendar-title → topic map, /üben booking flow
  state.py              srs.json + session.json (atomic, self-healing)
  tracker.py            Writes session summaries/mistakes to the vault
herrclaw_bridge/
  server.py             MCP Streamable-HTTP server on 127.0.0.1:8765 (7 tools, glue only)
  vault.py              Path-scoped, append-only Obsidian access (the allowlist IS the governance)
  reminders.py          Apple Reminders via osascript, pinned to the `Deutsch` list
  calendar_apple.py     EventKit freebusy/add (+ AppleScript fallback), pinned to `Deutsch Lernen`
  applescript.py        The single osascript gateway + AppleScript string escaping
  dates.py / errors.py  Local-time ISO parsing; BridgeError vs BridgeDenied
  audit.py              JSONL audit log for every governed call
state/                  Runtime state (gitignored): srs.json, session.json
tests/                  pytest suite for all of the above (143 tests)
```

A chat turn flows: input → command dispatch (direct answer, or message plus
optional system note — `/üben` books through the bridge here) → Nemotron
reply → markdown/reasoning sanitization → correction extraction → state
update; vault writes happen when corrections are logged and when the session
ends.

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
  agent-readable state. The bridge itself needs no secrets.
- **Network surface.** The tutor talks only to `integrate.api.nvidia.com`;
  the bridge is local-only on `127.0.0.1:8765`.

## Tests

```sh
uv run pytest
```

143 tests. The suite covers the chat loop, command dispatch (including
`/üben` booking against a fake bridge), correction parsing and
sanitization, SRS/streak/session state, the CLI, the vault allowlist
(including denial paths), the slot picker and topic map as pure functions,
AppleScript generation and escaping, the EventKit components against a fake
pyobjc-shaped EventKit (including the AppleScript fallback and denied
foreign calendars), and the MCP bridge end-to-end: a real uvicorn server +
real MCP client over HTTP with the Apple backends faked at module level —
no test ever touches the real vault, Reminders, Calendar, or
`~/.herr-claw`.

## Roadmap

1. **Voice mode** (`/sprechen`): mic → STT (mlx-whisper `base`, language
   locked to `de`, MacBook mic selected explicitly) → reply → TTS
   (`say -v Anna`, markdown stripped), round trip under 5 s. STT/TTS
   dependencies are already installed.
2. **Vocabulary quiz** (`/quiz`): seeded A1 word list + SRS scheduling,
   nouns always with article and plural.
3. **Telegram**: the same tutor over a token-gated, single-chat bot.
4. **Daily loop + governance evidence**: 08:00 nudge (freebusy → slot →
   Reminder → Telegram), 12:30 micro-quiz, 20:00 recap; sandbox network
   policy additions for the bridge endpoint.
