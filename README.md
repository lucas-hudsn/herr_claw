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
- **Learning state** in `state/`: mistake log (deduplicated with counts),
  daily streak, session/message/correction totals, and an SM-2-lite spaced
  repetition engine (L1→1d, L2→3d, L3→7d, L4→14d, L5→30d; a miss drops one
  level). Both `state/srs.json` and `state/session.json` are written
  atomically; a corrupt `srs.json` is backed up, never silently dropped.
- **Obsidian logging** through `herrclaw_bridge.vault`: append-only writes
  to `Deutsch/progress.md`, `Deutsch/fehler.md`, and
  `Daily notes/YYYY-MM-DD-deutsch.md`. If `HERR_VAULT` is unset, chat still
  works and only `state/` persists.
- **Audit trail**: every vault call — allowed *and* denied — lands as one
  JSON line in `~/.herr-claw/audit.log`.
- **Robust inference**: `thinking` disabled on every call (reasoning mode
  measured 9–54 s vs ~1.4 s without), retry with backoff on transient
  free-tier 503s, friendly German error messages, and reply sanitization
  that strips reasoning blocks and markdown so answers stay short and
  speakable (they will be read aloud).

Not yet: MCP bridge server + Apple Reminders/Calendar (next), voice mode,
vocabulary quiz, Telegram.

## Requirements

- macOS on Apple Silicon (uses `say`, later mlx-whisper and Apple apps)
- Python 3.14+ and [`uv`](https://docs.astral.sh/uv/)
- An NVIDIA Build API key (https://build.nvidia.com)
- Optional: an Obsidian vault for progress notes

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
| `HERR_VAULT` | Path to your Obsidian vault; unset disables vault writes |
| `HERR_MODEL` | Optional model override |
| `HERR_NVIDIA_BASE_URL` | Optional endpoint override |
| `HERR_WHISPER_MODEL` | Whisper model for voice mode (default `base`) |

## Usage

```sh
uv run herr-claw            # help
uv run herr-claw chat       # text chat in the terminal
```

In chat, talk to Herr Claw in German and use the commands:

| Command | What it does |
| --- | --- |
| `/üben <Thema>` | Start practicing a topic (e.g. `/üben Bäckerei`) |
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
  state.py              srs.json + session.json (atomic, self-healing)
  tracker.py            Writes session summaries/mistakes to the vault
herrclaw_bridge/
  vault.py              Path-scoped, append-only Obsidian access (the allowlist IS the governance)
  audit.py              JSONL audit log for every governed call
state/                  Runtime state (gitignored): srs.json, session.json
tests/                  pytest suite for all of the above
```

A chat turn flows: input → command dispatch (direct answer, or message plus
optional system note) → Nemotron reply → markdown/reasoning sanitization →
correction extraction → state update; vault writes happen when corrections
are logged and when the session ends.

## Safety and governance

- **One state path.** All persistent tutor state lives in `state/`; there is
  no second scheduling or storage mechanism.
- **Vault allowlist.** Writes go through `vault.append` only — there is no
  edit or delete API — and only inside `Deutsch/`, `Daily notes/`, and
  `Weeks/` of `HERR_VAULT`. Path traversal, separator escapes, and anything
  outside the allowlist raise `VaultDenied`. Every call, allowed or denied,
  is audited to `~/.herr-claw/audit.log`.
- **Secrets stay in the environment.** `NVIDIA_API_KEY` and
  `TELEGRAM_BOT_TOKEN` are read from the environment only (`.env` in
  host-fallback mode); they are never hardcoded, logged, or written into
  agent-readable state.
- **Network surface.** The tutor talks only to `integrate.api.nvidia.com`
  (and later `api.telegram.org`); the MCP bridge will be local-only on
  `127.0.0.1:8765`.

## Tests

```sh
uv run pytest
```

The suite covers the chat loop, command dispatch, correction parsing and
sanitization, SRS/streak/session state, the CLI, and the vault allowlist
(including denial paths). Tests never touch the real vault or
`~/.herr-claw` — audit log and vault root are redirected to temp dirs.

## Roadmap

1. **Bridge server**: expose the vault (plus Apple Reminders and Calendar)
   over Streamable-HTTP MCP on `localhost:8765`, behind the same audit trail.
2. **Voice mode** (`/sprechen`): mic → STT (Nemotron omni audio-in, else
   mlx-whisper `base`, language locked to `de`) → reply → TTS (omni
   audio-out, else `say -v Anna`), round trip under 5 s.
3. **Vocabulary quiz** (`/quiz`): seeded A1 word list + SRS scheduling,
   nouns always with article and plural.
4. **Telegram**: the same tutor over a token-gated, single-chat bot.
