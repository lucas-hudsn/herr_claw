"""Runtime configuration. Variable names and defaults follow .env.example.

ONE state path: everything persistent lives in <repo>/state/ (bind-mounted
to /sandbox/state/ when the NemoClaw sandbox is live — same code path).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = REPO_ROOT / "state"
SOUL_PATH = REPO_ROOT / "agent" / "SOUL.md"

DEFAULT_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"


@dataclass(frozen=True)
class Config:
    model: str
    base_url: str
    vault_root: Path | None
    state_dir: Path = DEFAULT_STATE_DIR

    @property
    def srs_path(self) -> Path:
        return self.state_dir / "srs.json"

    @property
    def session_path(self) -> Path:
        return self.state_dir / "session.json"


def load_config() -> Config:
    vault = os.environ.get("HERR_VAULT", "").strip()
    return Config(
        model=os.environ.get("HERR_MODEL", "").strip() or DEFAULT_MODEL,
        base_url=os.environ.get("HERR_NVIDIA_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        vault_root=Path(os.path.expanduser(vault)) if vault else None,
    )
