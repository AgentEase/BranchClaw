"""Shared fixtures for clawteam tests.

We redirect all file-based state to tmp_path so tests never touch the real ~/.clawteam.
"""

import socket

import pytest

from branchclaw.daemon import BranchClawDaemonClient, start_daemon_process


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point CLAWTEAM_DATA_DIR at a temp dir so every test gets a clean slate."""
    data_dir = tmp_path / ".clawteam"
    data_dir.mkdir()
    branchclaw_data_dir = tmp_path / ".branchclaw"
    branchclaw_data_dir.mkdir()
    branchclaw_daemon_root = tmp_path / ".branchclawd"
    branchclaw_daemon_root.mkdir()
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("BRANCHCLAW_DATA_DIR", str(branchclaw_data_dir))
    monkeypatch.setenv("BRANCHCLAW_DAEMON_ROOT", str(branchclaw_daemon_root))
    # Also override HOME so config_path() doesn't hit real ~/.clawteam/config.json
    monkeypatch.setenv("HOME", str(tmp_path))
    return data_dir


@pytest.fixture
def team_name():
    return "test-team"


@pytest.fixture
def branchclaw_daemon():
    """Start a test-isolated BranchClaw daemon and stop it after the test."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        _, port = sock.getsockname()
    start_daemon_process(timeout_seconds=10.0, host="127.0.0.1", port=port)
    try:
        yield
    finally:
        client = BranchClawDaemonClient.optional()
        if client is not None:
            client.stop()
