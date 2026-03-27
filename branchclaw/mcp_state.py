"""Global BranchClaw MCP server and token state helpers."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from branchclaw.config import get_data_dir, load_config
from branchclaw.models import ProjectProfile, WorkerMcpSession, new_id, now_iso
from branchclaw.storage import save_json
from clawteam.spawn.adapters import is_claude_command
from clawteam.spawn.command_validation import normalize_spawn_command

DEFAULT_ALLOWED_TOOLS = [
    "context.get_worker_context",
    "project.detect",
    "project.install_dependencies",
    "service.start_tmux",
    "service.discover_url",
    "service.stop_tmux",
    "diff.generate_architecture_summary",
    "worker.create_checkpoint",
    "worker.report_result",
]


def mcp_root() -> Path:
    path = get_data_dir() / "mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mcp_server_status_path() -> Path:
    return mcp_root() / "server.json"


def mcp_tokens_dir() -> Path:
    path = mcp_root() / "tokens"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mcp_token_path(token_id: str) -> Path:
    return mcp_tokens_dir() / f"{token_id}.json"


def worker_mcp_config_path(run_id: str, worker_name: str) -> Path:
    from branchclaw.runtime import worker_runtime_dir

    return worker_runtime_dir(run_id, worker_name) / "mcp-config.json"


def read_mcp_server_status() -> dict[str, Any]:
    path = mcp_server_status_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _normalize_data_dir(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _mcp_server_info(base_url: str, *, timeout_seconds: float = 1.5) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url}/healthz")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                return {}
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                return {}
            return payload
    except (urllib.error.URLError, TimeoutError, OSError):
        return {}


def _mcp_healthcheck(base_url: str, *, timeout_seconds: float = 1.5) -> bool:
    return bool(_mcp_server_info(base_url, timeout_seconds=timeout_seconds))


def _server_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _find_available_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _matching_server_info(base_url: str, *, data_dir: str) -> dict[str, Any]:
    info = _mcp_server_info(base_url)
    if not info:
        return {}
    remote_data_dir = info.get("data_dir")
    if not isinstance(remote_data_dir, str):
        return {}
    if _normalize_data_dir(remote_data_dir) != data_dir:
        return {}
    return info


def ensure_mcp_server(*, run_id: str = "") -> dict[str, Any]:
    config = load_config()
    host = config.mcp_host
    preferred_port = int(config.mcp_port)
    current_data_dir = _normalize_data_dir(get_data_dir())
    status = read_mcp_server_status()
    status_base_url = str(status.get("base_url", ""))
    status_info = _matching_server_info(status_base_url, data_dir=current_data_dir) if status_base_url else {}
    if status_base_url and status_info:
        payload = {
            "pid": int(status.get("pid", 0)) if _pid_alive(int(status.get("pid", 0))) else int(status_info.get("pid", 0)),
            "host": str(status.get("host", host)),
            "port": int(status.get("port", preferred_port)),
            "base_url": status_base_url,
            "started_at": status.get("started_at", now_iso()),
            "data_dir": current_data_dir,
        }
        save_json(mcp_server_status_path(), payload)
        return {**payload, "started": False, "reused": True}

    preferred_base_url = _server_base_url(host, preferred_port)
    preferred_info = _matching_server_info(preferred_base_url, data_dir=current_data_dir)
    if preferred_info:
        payload = {
            "pid": int(preferred_info.get("pid", 0)),
            "host": host,
            "port": preferred_port,
            "base_url": preferred_base_url,
            "started_at": status.get("started_at", now_iso()),
            "data_dir": current_data_dir,
        }
        save_json(mcp_server_status_path(), payload)
        return {**payload, "started": False, "reused": True}

    port = preferred_port
    if preferred_info == {} and port_in_use(host, preferred_port):
        port = _find_available_port(host)
    base_url = _server_base_url(host, port)

    env = dict(os.environ)
    env.pop("BRANCHCLAW_DAEMON_PROCESS", None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "branchclaw",
            "mcp",
            "serve-local",
            "--host",
            host,
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    deadline = time.time() + max(1.0, float(config.mcp_start_timeout))
    while time.time() < deadline:
        server_info = _matching_server_info(base_url, data_dir=current_data_dir)
        if server_info:
            payload = {
                "pid": int(server_info.get("pid", process.pid)) or process.pid,
                "host": host,
                "port": port,
                "base_url": base_url,
                "started_at": now_iso(),
                "data_dir": current_data_dir,
            }
            save_json(mcp_server_status_path(), payload)
            return {**payload, "started": True, "reused": False}
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for BranchClaw MCP server on {base_url}")


def command_supports_mcp(command: list[str], *, backend: str) -> bool:
    normalized = normalize_spawn_command(command)
    if backend != "tmux":
        return False
    if "--mcp-config" in normalized:
        return False
    return is_claude_command(normalized)


def create_worker_mcp_session(
    *,
    run_id: str,
    worker_name: str,
    stage_id: str,
    workspace_path: str,
    repo_root: str,
    project_profile: str | ProjectProfile,
    task: str = "",
    allowed_tools: list[str] | None = None,
    server_url: str = "",
) -> WorkerMcpSession:
    token_id = new_id("mcp-")
    token = f"{token_id}.{secrets.token_urlsafe(24)}"
    session = WorkerMcpSession(
        token_id=token_id,
        token=token,
        run_id=run_id,
        worker_name=worker_name,
        stage_id=stage_id,
        workspace_path=workspace_path,
        repo_root=repo_root,
        project_profile=ProjectProfile(project_profile),
        task=task,
        server_url=server_url,
        allowed_tools=list(allowed_tools or DEFAULT_ALLOWED_TOOLS),
    )
    save_json(mcp_token_path(token_id), json.loads(session.model_dump_json()))
    return session


def load_worker_mcp_session_from_token(token: str) -> WorkerMcpSession | None:
    token_id, _, _secret = token.partition(".")
    if not token_id:
        return None
    path = mcp_token_path(token_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        session = WorkerMcpSession.model_validate(payload)
    except Exception:
        return None
    if session.token != token or session.revoked_at:
        return None
    return session


def revoke_worker_mcp_session(
    token_id: str,
    *,
    run_id: str = "",
    reason: str = "",
) -> bool:
    path = mcp_token_path(token_id)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        session = WorkerMcpSession.model_validate(payload)
    except Exception:
        return False
    if session.revoked_at:
        return False
    session.revoked_at = now_iso()
    save_json(path, json.loads(session.model_dump_json()))
    actual_run_id = run_id or session.run_id
    if actual_run_id and active_mcp_session_count(run_id=actual_run_id) == 0:
        from branchclaw.storage import EventStore

        try:
            EventStore().append(
                actual_run_id,
                "mcp.server_stopped",
                {
                    "base_url": session.server_url,
                    "reason": reason or "no_active_workers",
                    "run_detached": True,
                },
            )
        except ValueError:
            pass
    return True


def active_mcp_session_count(*, run_id: str = "") -> int:
    count = 0
    for path in sorted(mcp_tokens_dir().glob("mcp-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            session = WorkerMcpSession.model_validate(payload)
        except Exception:
            continue
        if session.revoked_at:
            continue
        if run_id and session.run_id != run_id:
            continue
        count += 1
    return count


def write_worker_mcp_config(
    run_id: str,
    worker_name: str,
    *,
    server_url: str,
    token: str,
    server_name: str = "branchclaw-worker",
) -> Path:
    path = worker_mcp_config_path(run_id, worker_name)
    payload = {
        "mcpServers": {
            server_name: {
                "type": "http",
                "url": f"{server_url}/mcp",
                "headers": {
                    "Authorization": f"Bearer {token}",
                },
            }
        }
    }
    save_json(path, payload)
    return path


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0
