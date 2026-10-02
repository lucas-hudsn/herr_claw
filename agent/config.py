"""Runtime configuration. Variable names and defaults follow .env.example.

ONE state path: everything persistent lives in the directory named by
HERR_STATE_DIR — <repo>/state/ on the host, /sandbox/herrclaw/state inside
the sandbox (P6: the agent's brain runs IN the sandbox, so its state lives
there too; the host keeps no agent state). This file is the single source
of paths on both sides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from herrclaw_bridge.server import DEFAULT_BRIDGE_URL

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = REPO_ROOT / "state"
SOUL_PATH = REPO_ROOT / "agent" / "SOUL.md"

DEFAULT_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_WHISPER_MODEL = "base"  # never tiny — too weak for German (AGENTS.md)
BRIDGE_OFF = {"off", "false", "0", "none"}
CHAT_BACKEND_LOCAL = "local"  # HERR_CHAT_BACKEND — break-glass in-process brain
SANDBOX_STATE_DIR = "/sandbox/herrclaw/state"


@dataclass(frozen=True)
class Config:
    model: str
    base_url: str
    vault_root: Path | None
    bridge_url: str | None = None
    state_dir: Path = DEFAULT_STATE_DIR
    whisper_model: str = DEFAULT_WHISPER_MODEL
    mic_device: str = ""  # HERR_MIC_DEVICE override (name substring or index)
    telegram_chat_id: str = ""  # HERR_TELEGRAM_CHAT_ID — the ONE chat the bot answers
    chat_backend: str = "sandbox"  # "sandbox" (default) | "local" (break-glass)

    @property
    def srs_path(self) -> Path:
        return self.state_dir / "srs.json"

    @property
    def session_path(self) -> Path:
        return self.state_dir / "session.json"

    @property
    def memory_path(self) -> Path:
        return self.state_dir / "memory.md"


def load_config() -> Config:
    vault = os.environ.get("HERR_VAULT", "").strip()
    bridge = os.environ.get("HERR_BRIDGE_URL", "").strip()
    state_dir = os.environ.get("HERR_STATE_DIR", "").strip()
    backend = os.environ.get("HERR_CHAT_BACKEND", "").strip().lower()
    return Config(
        model=os.environ.get("HERR_MODEL", "").strip() or DEFAULT_MODEL,
        base_url=os.environ.get("HERR_NVIDIA_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        vault_root=Path(os.path.expanduser(vault)) if vault else None,
        # HERR_BRIDGE_URL empty → default endpoint; off/false/0/none → chat without bridge.
        bridge_url=None if bridge.lower() in BRIDGE_OFF else (bridge or DEFAULT_BRIDGE_URL),
        whisper_model=os.environ.get("HERR_WHISPER_MODEL", "").strip() or DEFAULT_WHISPER_MODEL,
        mic_device=os.environ.get("HERR_MIC_DEVICE", "").strip(),
        telegram_chat_id=os.environ.get("HERR_TELEGRAM_CHAT_ID", "").strip(),
        chat_backend=backend if backend in {"sandbox", CHAT_BACKEND_LOCAL} else "sandbox",
        state_dir=Path(os.path.expanduser(state_dir)) if state_dir else DEFAULT_STATE_DIR,
    )
