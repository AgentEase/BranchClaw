"""Configuration helpers for BranchClaw."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel


class BranchClawConfig(BaseModel):
    data_dir: str = ""
    default_backend: str = "tmux"
    default_agent_command: str = "claude"
    skip_permissions: bool = False
    board_host: str = "127.0.0.1"
    board_port: int = 8090
    heartbeat_interval: float = 1.0
    stale_after: float = 5.0
    worker_block_after: float = 300.0
    worker_tool_retry_limit: int = 3
    worker_auto_remediation_limit: int = 2
    worker_auto_restart_limit: int = 1
    daemon_watchdog_interval: float = 5.0
    supervisor_start_timeout: float = 10.0
    claude_ready_timeout: float = 30.0
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765
    mcp_start_timeout: float = 10.0


def config_path() -> Path:
    """Return the config path under the BranchClaw home directory."""
    return Path.home() / ".branchclaw" / "config.json"


def load_config() -> BranchClawConfig:
    path = config_path()
    if path.exists():
        try:
            config = BranchClawConfig.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except Exception:
            config = BranchClawConfig()
    else:
        config = BranchClawConfig()

    env_overrides: dict[str, tuple[str, type]] = {
        "BRANCHCLAW_DATA_DIR": ("data_dir", str),
        "BRANCHCLAW_DEFAULT_BACKEND": ("default_backend", str),
        "BRANCHCLAW_DEFAULT_AGENT_COMMAND": ("default_agent_command", str),
        "BRANCHCLAW_SKIP_PERMISSIONS": ("skip_permissions", lambda value: value.lower() not in ("0", "false", "no", "")),
        "BRANCHCLAW_BOARD_HOST": ("board_host", str),
        "BRANCHCLAW_BOARD_PORT": ("board_port", int),
        "BRANCHCLAW_HEARTBEAT_INTERVAL": ("heartbeat_interval", float),
        "BRANCHCLAW_STALE_AFTER": ("stale_after", float),
        "BRANCHCLAW_WORKER_BLOCK_AFTER": ("worker_block_after", float),
        "BRANCHCLAW_WORKER_TOOL_RETRY_LIMIT": ("worker_tool_retry_limit", int),
        "BRANCHCLAW_WORKER_AUTO_REMEDIATION_LIMIT": ("worker_auto_remediation_limit", int),
        "BRANCHCLAW_WORKER_AUTO_RESTART_LIMIT": ("worker_auto_restart_limit", int),
        "BRANCHCLAW_DAEMON_WATCHDOG_INTERVAL": ("daemon_watchdog_interval", float),
        "BRANCHCLAW_SUPERVISOR_START_TIMEOUT": ("supervisor_start_timeout", float),
        "BRANCHCLAW_CLAUDE_READY_TIMEOUT": ("claude_ready_timeout", float),
        "BRANCHCLAW_MCP_HOST": ("mcp_host", str),
        "BRANCHCLAW_MCP_PORT": ("mcp_port", int),
        "BRANCHCLAW_MCP_START_TIMEOUT": ("mcp_start_timeout", float),
    }
    for env_name, (field, caster) in env_overrides.items():
        raw = os.environ.get(env_name)
        if raw in {None, ""}:
            continue
        try:
            setattr(config, field, caster(raw))
        except ValueError:
            continue
    return config


def get_data_dir() -> Path:
    """Return the data directory, honoring env and config."""
    custom = os.environ.get("BRANCHCLAW_DATA_DIR")
    if not custom:
        custom = load_config().data_dir or None
    root = Path(custom) if custom else Path.home() / ".branchclaw"
    root.mkdir(parents=True, exist_ok=True)
    return root
