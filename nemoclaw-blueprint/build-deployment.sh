#!/usr/bin/env bash
# Herr Claw — rebuild the mac-mini NemoClaw deployment step by step.
#
# This is THE reproducible path for the submission's primary mode: an
# OpenClaw sandbox (NemoClaw/OpenShell) that IS the agent (P6). Its cron
# fires `herr-claw trigger <job>` inside a vendored, offline trigger venv,
# and the job body executes IN THE SANDBOX against /sandbox/herrclaw/state;
# the sandboxed brain also serves every TUI/voice turn (via `nemoclaw exec`
# → `herr-claw turn`) and the Telegram conversation (`telegram-loop`).
# Apple/vault access crosses the host bridge (127.0.0.1:8765, reachable as
# host.openshell.internal:8765) — allowlisted and audited there.
#
# What each phase does (details + example output: nemoclaw-blueprint/DEPLOYMENT.md):
#   0  preflight        — CLIs, Docker, sandbox record, .env key NAMES (never values)
#   1  build            — `uv build --wheel` for the app (pure-python wheel)
#   2  vendor           — linux-aarch64 wheels for the core deps (sandbox NEVER talks to pypi)
#   3  sandbox          — start the NemoClaw sandbox
#   4  upload           — wheel + vendored wheels + source (agent/, herrclaw_bridge/,
#                          main.py, pyproject.toml, seed/) + the sandbox .env
#                          (non-secret endpoints + the token PLACEHOLDER —
#                          deliberately NOT voice/: macOS-only frontends)
#   5  install          — extract wheels into the sandbox venv offline (the venv has no pip;
#                          console script `herr-claw` is written by hand)
#   6  policy           — apply nemoclaw-blueprint/policy-additions.yaml (bridge endpoint,
#                          verb-scoped /mcp); caller pin per the yaml header (openshell policy set)
#   7  bridge + cron    — host bridge on 127.0.0.1:8765 (alias allowlist set), then
#                          `herr-claw install-cron` (the ONE scheduler + watchdog entries)
#   8  verify           — doctor, policy version, bridge reachability, cron list
#   --smoke             — OPTIONAL: one REAL `trigger quiz` fire end to end.
#                          WARNING: sends REAL Telegram messages and books REAL
#                          Apple Calendar/Reminders entries (and heals missed earlier
#                          jobs of the day — that is the design, not a bug).
#
# Idempotent by construction: rebuilds are safe; session.json dedup means a
# job can never fire twice on one day no matter how often you run this.
set -euo pipefail

SANDBOX="${HERR_NEMOCLAW_SANDBOX:-my-assistant}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="/sandbox/herrclaw"
WHEELS_DIR="/sandbox/herrclaw-wheels"
BRIDGE_ALIAS="host.openshell.internal:8765"
SMOKE=0
[[ "${1:-}" == "--smoke" ]] && SMOKE=1

step() { printf '\n━━━ %s ━━━\n' "$*"; }
warn() { printf '⚠︎  %s\n' "$*"; }

# ── 0/8 preflight ────────────────────────────────────────────────────────────
step "0/8 Preflight"
for cmd in uv nemoclaw openshell docker python3 pip3 curl; do
  if ! command -v "$cmd" >/dev/null; then
    warn "missing: $cmd"
    if [[ "$cmd" == "nemoclaw" || "$cmd" == "openshell" ]]; then
      warn "install NemoClaw from NVIDIA first: curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash, then 'nemoclaw onboard'"
    fi
    exit 1
  fi
done
docker info >/dev/null 2>&1 || { warn "Docker daemon is not running — start Docker Desktop first."; exit 1; }
nemoclaw list 2>/dev/null | grep -q "$SANDBOX" || {
  warn "sandbox '$SANDBOX' not found — run \`nemoclaw onboard\` first (see DEPLOYMENT.md §prerequisites)."
  exit 1
}
echo "CLIs ok · Docker up · sandbox '$SANDBOX' exists"
warn "the host .env must define (names only — this script never reads values):"
warn "  NVIDIA_API_KEY · TELEGRAM_BOT_TOKEN · HERR_TELEGRAM_CHAT_ID · HERR_VAULT"
warn "  HERR_BRIDGE_ALLOWED_HOSTS=$BRIDGE_ALIAS · HERR_BRIDGE_ALLOWED_ORIGINS=$BRIDGE_ALIAS"

# ── 1/8 build ────────────────────────────────────────────────────────────────
step "1/8 Build the app wheel"
(cd "$REPO" && uv build --wheel >/dev/null)
WHEEL="$(ls -t "$REPO"/dist/herr_claw-*.whl | head -1)"
echo "built: $(basename "$WHEEL")"

# ── 2/8 vendor linux-aarch64 wheels ─────────────────────────────────────────
step "2/8 Vendor linux-aarch64 wheels (sandbox never talks to pypi)"
REQS="$(mktemp)"
(cd "$REPO" && uv export --no-hashes --no-emit-project --no-dev --no-extra bridge --no-extra voice -o "$REQS" >/dev/null)
pip3 download -r "$REQS" \
  --platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 --platform any \
  --implementation cp --python-version 3.14 --only-binary=:all: \
  -d "$REPO/nemoclaw-blueprint/wheels" >/dev/null
rm -f "$REQS"
echo "vendored: $(ls "$REPO/nemoclaw-blueprint/wheels" | wc -l | tr -d ' ') wheels in nemoclaw-blueprint/wheels/"

# ── 3/8 sandbox ──────────────────────────────────────────────────────────────
step "3/8 Start the sandbox"
nemoclaw "$SANDBOX" start 2>&1 | tail -2 \
  || warn "start failed — if the phase is stuck at Error, see DEPLOYMENT.md §troubleshooting"

# ── 4/8 upload ───────────────────────────────────────────────────────────────
step "4/8 Upload wheel + vendored wheels + brain source"
nemoclaw "$SANDBOX" upload "$WHEEL" "$WHEELS_DIR/" >/dev/null
nemoclaw "$SANDBOX" upload "$REPO/nemoclaw-blueprint/wheels" "$WHEELS_DIR/" >/dev/null 2>&1 \
  || warn "wheels dir upload skipped (already present?)"
for src in agent herrclaw_bridge main.py pyproject.toml seed; do
  nemoclaw "$SANDBOX" upload "$REPO/$src" "$APP_DIR/" >/dev/null
done
echo "uploaded: app wheel, vendored wheels, agent/ + herrclaw_bridge/ + main.py + pyproject.toml + seed/"
echo "          (voice/ deliberately NOT uploaded — macOS-only frontend, never the brain)"
# The sandbox .env carries NON-SECRET endpoints + the token PLACEHOLDER only
# (the gateway resolves it at egress; no key value ever lands in the sandbox).
nemoclaw "$SANDBOX" exec --no-tty -- bash -c "cat > $APP_DIR/.env <<'EOF'
HERR_BRIDGE_URL=http://host.openshell.internal:8765/mcp
HERR_STATE_DIR=/sandbox/herrclaw/state
HERR_NVIDIA_BASE_URL=https://inference.local/v1
# NOTE: the token placeholder must be the full gateway-issued
# credential-binding form (see TELEGRAM_TOKEN_PLACEHOLDER in
# agent/cron_sync.py) — the short openshell:resolve:env:TELEGRAM_BOT_TOKEN
# form does NOT resolve at egress (gateway 500).
TELEGRAM_BOT_TOKEN=openshell:resolve:env:v8392926173393239856_TELEGRAM_BOT_TOKEN
EOF"
echo "wrote $APP_DIR/.env (bridge alias, state dir, inference.local, token placeholder — no secrets)"

# ── 5/8 install ──────────────────────────────────────────────────────────────
step "5/8 Install into the trigger venv (offline — the venv has no pip)"
nemoclaw "$SANDBOX" exec --no-tty -- bash -c '
set -e
PY=/home/linuxbrew/.linuxbrew/Cellar/python@3.14/*/bin/python3.14
PYBIN=$(ls $PY 2>/dev/null | head -1)
[ -n "$PYBIN" ] || { echo "brew python3.14 not found"; exit 1; }
if [ ! -x /sandbox/herrclaw/.venv/bin/python ]; then
  "$PYBIN" -m venv --without-pip /sandbox/herrclaw/.venv
  echo "fresh venv created (no pip — offline by design)"
fi
SP=$(ls -d /sandbox/herrclaw/.venv/lib/python3.14/site-packages)
WORK=$(mktemp -d)
# first build: extract every vendored wheel (binary deps included);
# update:    the herr_claw wheel alone (deps are already in place)
if [ "$(ls "$SP" | grep -c -E "^(mcp|openai|pydantic|typer|yaml)" )" -eq 0 ]; then
  set -- /sandbox/herrclaw-wheels/*.whl
else
  set -- /sandbox/herrclaw-wheels/herr_claw-*.whl
fi
for w in "$@"; do
  rm -rf "$WORK/x" && mkdir -p "$WORK/x"
  "$PYBIN" -m zipfile -e "$w" "$WORK/x/"
  cp -a "$WORK/x/." "$SP/"
done
rm -rf "$WORK"
# console script (what a pip install would have generated)
cat > /sandbox/herrclaw/.venv/bin/herr-claw <<EOF
#!/sandbox/herrclaw/.venv/bin/python
import sys
from main import cli
sys.exit(cli())
EOF
chmod +x /sandbox/herrclaw/.venv/bin/herr-claw
/sandbox/herrclaw/.venv/bin/herr-claw --help >/dev/null && echo "trigger venv ok: \$(herr-claw --help) works offline"
'

# ── 6/8 policy ───────────────────────────────────────────────────────────────
step "6/8 Apply the network policy (bridge endpoint, /mcp verbs only)"
PIN_PRESENT=$(openshell policy get "$SANDBOX" --full -o json 2>/dev/null | grep -c herrclaw-bridge || true)
if [ "${PIN_PRESENT:-0}" -ge 1 ]; then
  echo "bridge policy already live — leaving the caller pin untouched (first-build-only step;"
  echo "re-merging the preset could normalize the binaries pin away)"
else
  nemoclaw "$SANDBOX" policy add --from-file "$REPO/nemoclaw-blueprint/policy-additions.yaml" --dry-run
  nemoclaw "$SANDBOX" policy add --from-file "$REPO/nemoclaw-blueprint/policy-additions.yaml" --yes
  warn "now apply the caller pin (allowed_ips + binaries, incl. the OpenClaw runtime) — it cannot"
  warn "go through nemoclaw's merge: follow the recipe in the header of"
  warn "nemoclaw-blueprint/policy-additions.yaml (\`openshell policy set\`), then verify with"
  warn "\`openshell policy get $SANDBOX --full -o json\`"
fi

# ── 7/8 bridge + cron ───────────────────────────────────────────────────────
step "7/8 Host bridge (127.0.0.1:8765) + the ONE scheduler"
if curl -s --max-time 2 -o /dev/null http://127.0.0.1:8765/mcp; then
  echo "bridge already up"
else
  warn "starting bridge in background (log: .bridge.log) — secrets come from .env via the app"
  (cd "$REPO" && nohup env HERR_BRIDGE_ALLOWED_HOSTS="$BRIDGE_ALIAS" \
    HERR_BRIDGE_ALLOWED_ORIGINS="$BRIDGE_ALIAS" \
    uv run herr-claw bridge > .bridge.log 2>&1 &)
  sleep 3
  curl -s --max-time 2 -o /dev/null http://127.0.0.1:8765/mcp && echo "bridge up"
fi
(cd "$REPO" && uv run herr-claw install-cron)

# ── 8/8 verify ───────────────────────────────────────────────────────────────
step "8/8 Verify"
openshell policy get "$SANDBOX" 2>/dev/null | grep -E "active_version|version" || true
nemoclaw "$SANDBOX" doctor 2>&1 | grep -E "status|Sandbox|Inference|fail" | head -8 || true
if [ "$SMOKE" -eq 1 ]; then
  warn "--smoke: one REAL trigger fire (real Telegram, real calendar booking;"
  warn "missed earlier jobs of the day are caught up by design)"
  nemoclaw "$SANDBOX" exec --no-tty --timeout 150 -- \
    /sandbox/herrclaw/.venv/bin/herr-claw trigger quiz || true
  echo "--- audit tail (~/.herr-claw/audit.log):"
  tail -3 ~/.herr-claw/audit.log
else
  echo "dry run complete — rerun with --smoke for one real end-to-end trigger fire"
fi
step "Done — see nemoclaw-blueprint/DEPLOYMENT.md for what each step proved"
