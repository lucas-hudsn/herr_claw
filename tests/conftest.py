"""Shared fixtures. Tests never touch ~/.herr-claw or the real vault —
audit log and vault root are pointed at tmp_path."""

import pytest

from agent.config import Config
from agent.state import SessionState, SrsState
from herrclaw_bridge.vault import Vault


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        model="test-model",
        base_url="http://localhost:1/v1",
        vault_root=tmp_path / "vault",
        state_dir=tmp_path / "state",
    )


@pytest.fixture
def srs(config) -> SrsState:
    return SrsState(config.srs_path)


@pytest.fixture
def session(config) -> SessionState:
    return SessionState.load(config.session_path)


@pytest.fixture
def vault(config, tmp_path) -> Vault:
    return Vault(config.vault_root, audit_log=tmp_path / "audit.log")
