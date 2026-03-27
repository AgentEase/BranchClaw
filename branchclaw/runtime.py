"""Worker supervision helpers for BranchClaw."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from branchclaw.config import get_data_dir, load_config
from branchclaw.mcp_state import revoke_worker_mcp_session, write_worker_mcp_config
from branchclaw.models import WorkerRuntime, WorkerStatus, now_iso
from branchclaw.storage import EventStore, save_json
from branchclaw.workspace import GitWorkspaceRuntimeAdapter
from clawteam.spawn.adapters import NativeCliAdapter
from clawteam.spawn.command_validation import (
    is_claude_command,
    is_codex_command,
    is_gemini_command,
    validate_spawn_command,
)


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "-" for char in value).strip("-") or "item"


def worker_runtime_dir(run_id: str, worker_name: str) -> Path:
    path = get_data_dir() / "runs" / run_id / "runtime" / _slug(worker_name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def worker_launch_path(run_id: str, worker_name: str) -> Path:
    return worker_runtime_dir(run_id, worker_name) / "launch.json"


def worker_status_path(run_id: str, worker_name: str) -> Path:
    return worker_runtime_dir(run_id, worker_name) / "status.json"


def worker_stop_path(run_id: str, worker_name: str) -> Path:
    return worker_runtime_dir(run_id, worker_name) / "stop"


def clear_worker_runtime_state(run_id: str, worker_name: str) -> None:
    runtime_dir = worker_runtime_dir(run_id, worker_name)
    for child in runtime_dir.iterdir():
        if child.is_file():
            child.unlink(missing_ok=True)


def read_worker_status(run_id: str, worker_name: str) -> dict[str, Any]:
    path = worker_status_path(run_id, worker_name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_worker_status(run_id: str, worker_name: str, payload: dict[str, Any]) -> None:
    save_json(worker_status_path(run_id, worker_name), payload)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tmux_target_alive(target: str) -> bool:
    if not target or not shutil.which("tmux"):
        return False
    result = subprocess.run(
        ["tmux", "list-panes", "-t", target, "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def tmux_target_pid(target: str) -> int:
    if not target or not shutil.which("tmux"):
        return 0
    result = subprocess.run(
        ["tmux", "list-panes", "-t", target, "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        return 0


def terminate_tmux_target(target: str) -> None:
    if not target or not shutil.which("tmux"):
        return
    subprocess.run(
        ["tmux", "kill-window", "-t", target],
        capture_output=True,
        text=True,
    )


def launch_supervised_worker(run_id: str, worker_name: str) -> int:
    payload = read_worker_launch(run_id, worker_name)
    runtime_dir = worker_runtime_dir(run_id, worker_name)
    stop_path = worker_stop_path(run_id, worker_name)
    stop_path.unlink(missing_ok=True)

    store = EventStore()
    adapter = GitWorkspaceRuntimeAdapter(payload["repo_root"])
    config = load_config()
    heartbeat_interval = max(0.1, float(payload.get("heartbeat_interval", config.heartbeat_interval)))
    stop_requested = False

    def _request_stop(*_: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    base_env = {**os.environ, **payload.get("env", {})}
    base_env["PATH"] = os.environ.get("PATH", "")
    launch_meta = _launch_child(
        command=payload["command"],
        backend=payload["backend"],
        cwd=payload["workspace_path"],
        prompt=payload["prompt"],
        system_prompt=str(payload.get("system_prompt", "")),
        env=base_env,
        worker_name=worker_name,
        run_id=run_id,
        skip_permissions=bool(payload.get("skip_permissions", config.skip_permissions)),
        ready_timeout=float(payload.get("claude_ready_timeout", config.claude_ready_timeout)),
        mcp_enabled=bool(payload.get("mcp_enabled")),
        mcp_server_url=str(payload.get("mcp_server_url", "")),
        mcp_token=str(payload.get("mcp_token", "")),
    )
    if launch_meta.get("error"):
        if payload.get("mcp_token_id"):
            revoke_worker_mcp_session(
                str(payload["mcp_token_id"]),
                run_id=run_id,
                reason="launch_failed",
            )
        write_worker_status(
            run_id,
            worker_name,
            {
                "status": "launch_failed",
                "error": launch_meta["error"],
                "supervisor_pid": os.getpid(),
                "finished_at": now_iso(),
            },
        )
        return 1

    started_at = now_iso()
    child_pid = int(launch_meta.get("child_pid", 0))
    tmux_target = launch_meta.get("tmux_target", "")
    worker = WorkerRuntime(
        worker_name=worker_name,
        run_id=run_id,
        stage_id=payload["stage_id"],
        feature_id=str(payload.get("feature_id", "")),
        workspace_path=payload["workspace_path"],
        branch=payload["branch"],
        base_ref=payload["base_ref"],
        head_sha=_safe_head_sha(adapter, payload["workspace_path"]),
        backend=payload["backend"],
        pid=child_pid,
        child_pid=child_pid,
        supervisor_pid=os.getpid(),
        tmux_target=tmux_target,
        task=payload.get("task", ""),
        heartbeat_at=started_at,
        last_heartbeat_at=started_at,
        started_at=started_at,
        status=WorkerStatus.running,
        mcp_enabled=bool(payload.get("mcp_enabled")),
        mcp_server_url=str(payload.get("mcp_server_url", "")),
        mcp_token_id=str(payload.get("mcp_token_id", "")),
        remediation_attempt_count=int(payload.get("remediation_attempt_count", 0) or 0),
        restart_attempt_count=int(payload.get("restart_attempt_count", 0) or 0),
        managed_by_daemon=bool(payload.get("managed_by_daemon")),
        daemon_pid=int(payload.get("daemon_pid", 0) or 0),
    )
    store.append(run_id, "worker.started", {"worker": json.loads(worker.model_dump_json())})
    _emit_heartbeat(store, adapter, worker)
    write_worker_status(
        run_id,
        worker_name,
        {
            "status": "running",
            "backend": payload["backend"],
            "child_pid": child_pid,
            "supervisor_pid": os.getpid(),
            "tmux_target": tmux_target,
            "started_at": started_at,
            "last_heartbeat_at": started_at,
            "head_sha": worker.head_sha,
            "runtime_dir": str(runtime_dir),
            "mcp_enabled": bool(payload.get("mcp_enabled")),
            "mcp_server_url": payload.get("mcp_server_url", ""),
            "mcp_token_id": payload.get("mcp_token_id", ""),
            "managed_by_daemon": bool(payload.get("managed_by_daemon")),
            "daemon_pid": int(payload.get("daemon_pid", 0) or 0),
        },
    )

    explicit_stop = False
    exit_code = 0
    failure_reason = ""
    while True:
        if stop_requested or stop_path.exists():
            explicit_stop = True
            _terminate_child(
                backend=payload["backend"],
                process=launch_meta.get("process"),
                tmux_target=tmux_target,
            )
            stop_requested = True

        current_exit = _poll_child(
            backend=payload["backend"],
            process=launch_meta.get("process"),
            tmux_target=tmux_target,
        )
        if current_exit is not None:
            exit_code = current_exit
            break

        head_sha = _emit_heartbeat(store, adapter, worker)
        write_worker_status(
            run_id,
            worker_name,
            {
                "status": "running",
                "backend": payload["backend"],
                "child_pid": child_pid,
                "supervisor_pid": os.getpid(),
                "tmux_target": tmux_target,
                "started_at": started_at,
                "last_heartbeat_at": now_iso(),
                "head_sha": head_sha,
                "runtime_dir": str(runtime_dir),
                "managed_by_daemon": bool(payload.get("managed_by_daemon")),
                "daemon_pid": int(payload.get("daemon_pid", 0) or 0),
            },
        )
        time.sleep(heartbeat_interval)

    finished_at = now_iso()
    exit_payload = {
        "worker_name": worker_name,
        "child_pid": 0,
        "supervisor_pid": os.getpid(),
        "tmux_target": tmux_target,
        "exit_code": exit_code,
        "finished_at": finished_at,
        "explicit_stop": explicit_stop,
    }
    store.append(run_id, "worker.exited", exit_payload)

    if explicit_stop or exit_code == 0:
        store.append(
            run_id,
            "worker.stopped",
            {
                **exit_payload,
                "failure_reason": "",
            },
        )
        final_status = "stopped"
    else:
        failure_reason = f"worker exited with code {exit_code}"
        store.append(
            run_id,
            "worker.failed",
            {
                **exit_payload,
                "failure_reason": failure_reason,
            },
        )
        final_status = "failed"

    write_worker_status(
        run_id,
        worker_name,
        {
            "status": final_status,
            "backend": payload["backend"],
            "child_pid": 0,
            "supervisor_pid": os.getpid(),
            "tmux_target": tmux_target,
            "started_at": started_at,
            "finished_at": finished_at,
            "last_heartbeat_at": read_worker_status(run_id, worker_name).get("last_heartbeat_at", started_at),
            "head_sha": _safe_head_sha(adapter, payload["workspace_path"]),
            "exit_code": exit_code,
            "failure_reason": failure_reason,
            "explicit_stop": explicit_stop,
            "runtime_dir": str(runtime_dir),
            "mcp_enabled": bool(payload.get("mcp_enabled")),
            "mcp_server_url": payload.get("mcp_server_url", ""),
            "mcp_token_id": payload.get("mcp_token_id", ""),
            "managed_by_daemon": bool(payload.get("managed_by_daemon")),
            "daemon_pid": int(payload.get("daemon_pid", 0) or 0),
        },
    )
    if payload.get("mcp_token_id"):
        revoke_worker_mcp_session(
            str(payload["mcp_token_id"]),
            run_id=run_id,
            reason="worker_exit",
        )
    return 0 if explicit_stop or exit_code == 0 else exit_code


def read_worker_launch(run_id: str, worker_name: str) -> dict[str, Any]:
    path = worker_launch_path(run_id, worker_name)
    if not path.exists():
        raise RuntimeError(f"Missing worker launch payload for '{worker_name}'")
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_head_sha(adapter: GitWorkspaceRuntimeAdapter, workspace_path: str) -> str:
    try:
        return adapter.head_sha(workspace_path)
    except Exception:
        return ""


def _emit_heartbeat(store: EventStore, adapter: GitWorkspaceRuntimeAdapter, worker: WorkerRuntime) -> str:
    head_sha = _safe_head_sha(adapter, worker.workspace_path)
    timestamp = now_iso()
    store.append(
        worker.run_id,
        "worker.heartbeat",
        {
            "worker_name": worker.worker_name,
            "head_sha": head_sha,
            "child_pid": worker.child_pid,
            "supervisor_pid": worker.supervisor_pid,
            "tmux_target": worker.tmux_target,
            "last_heartbeat_at": timestamp,
        },
    )
    return head_sha


def _launch_child(
    *,
    command: list[str],
    backend: str,
    cwd: str,
    prompt: str,
    system_prompt: str = "",
    env: dict[str, str],
    worker_name: str,
    run_id: str,
    skip_permissions: bool,
    ready_timeout: float,
    mcp_enabled: bool,
    mcp_server_url: str,
    mcp_token: str,
) -> dict[str, Any]:
    adapter = NativeCliAdapter()
    session_args: list[str] = []
    if mcp_enabled and is_claude_command(command):
        config_path = write_worker_mcp_config(
            run_id,
            worker_name,
            server_url=mcp_server_url,
            token=mcp_token,
        )
        session_args = ["--mcp-config", str(config_path), "--strict-mcp-config"]
    prepared = adapter.prepare_command(
        command,
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=cwd,
        skip_permissions=skip_permissions,
        interactive=backend == "tmux",
        session_args=session_args,
    )
    command_error = validate_spawn_command(
        prepared.normalized_command,
        path=env.get("PATH"),
        cwd=cwd,
    )
    if command_error:
        return {"error": command_error}

    if backend == "subprocess":
        process = subprocess.Popen(
            prepared.final_command,
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"process": process, "child_pid": process.pid, "tmux_target": ""}

    if backend != "tmux":
        return {"error": f"Unsupported backend '{backend}'"}

    if not shutil.which("tmux"):
        return {"error": "tmux is not installed"}

    session_name = f"branchclaw-{run_id[:12]}"
    target = f"{session_name}:{worker_name}"
    export_clause = "; ".join(
        f"export {key}={shlex.quote(value)}"
        for key, value in env.items()
        if key.startswith("BRANCHCLAW_")
    )
    cmd_str = " ".join(shlex.quote(item) for item in prepared.final_command)
    full_cmd = f"{export_clause}; cd {shlex.quote(cwd)} && {cmd_str}"

    has_session = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if has_session.returncode == 0:
        launch = subprocess.run(
            ["tmux", "new-window", "-t", session_name, "-n", worker_name, full_cmd],
            capture_output=True,
            text=True,
        )
    else:
        launch = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-n", worker_name, full_cmd],
            capture_output=True,
            text=True,
        )
    if launch.returncode != 0:
        return {"error": launch.stderr.strip() or "Failed to launch tmux worker"}

    time.sleep(0.3)
    pane_check = subprocess.run(
        ["tmux", "list-panes", "-t", target, "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
    )
    if pane_check.returncode != 0 or not pane_check.stdout.strip():
        return {
            "error": (
                f"agent command '{prepared.normalized_command[0]}' exited immediately after launch. "
                "Verify the CLI works standalone before using it with branchclaw."
            )
        }

    _confirm_permission_bypass_if_prompted(target, prepared.normalized_command)
    _confirm_claude_theme_if_prompted(target, prepared.normalized_command)
    _confirm_workspace_trust_if_prompted(target, prepared.normalized_command)

    if prepared.post_launch_prompt and is_claude_command(prepared.normalized_command):
        _wait_for_claude_ready(target, timeout_seconds=ready_timeout)
        _paste_tmux_prompt(target, worker_name, prepared.post_launch_prompt)

    return {"process": None, "child_pid": tmux_target_pid(target), "tmux_target": target}


def _poll_child(
    *,
    backend: str,
    process: subprocess.Popen[str] | None,
    tmux_target: str,
) -> int | None:
    if backend == "subprocess":
        if process is None:
            return 1
        return process.poll()
    if backend == "tmux":
        return None if tmux_target_alive(tmux_target) else 0
    return 1


def _terminate_child(
    *,
    backend: str,
    process: subprocess.Popen[str] | None,
    tmux_target: str,
) -> None:
    if backend == "subprocess":
        if process is None:
            return
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        return

    if backend == "tmux":
        terminate_tmux_target(tmux_target)


def launch_supervisor_process(run_id: str, worker_name: str) -> subprocess.Popen[str]:
    command = [sys.executable, "-m", "branchclaw", "worker", "supervise", run_id, worker_name]
    env = dict(os.environ)
    env.pop("BRANCHCLAW_DAEMON_PROCESS", None)
    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


def _confirm_permission_bypass_if_prompted(
    target: str,
    command: list[str],
    *,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.5,
) -> bool:
    if not is_claude_command(command):
        return False

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pane = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", target],
            capture_output=True,
            text=True,
        )
        pane_text = pane.stdout.lower() if pane.returncode == 0 else ""
        if _looks_like_permission_bypass_prompt(command, pane_text):
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "Down"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.2)
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "Enter"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(1.0)
            return True

        time.sleep(poll_interval_seconds)

    return False


def _confirm_workspace_trust_if_prompted(
    target: str,
    command: list[str],
    *,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.5,
) -> bool:
    if not (is_claude_command(command) or is_codex_command(command) or is_gemini_command(command)):
        return False

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pane = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", target],
            capture_output=True,
            text=True,
        )
        pane_text = pane.stdout.lower() if pane.returncode == 0 else ""
        if _looks_like_workspace_trust_prompt(command, pane_text):
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "Enter"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.5)
            return True

        time.sleep(poll_interval_seconds)

    return False


def _confirm_claude_theme_if_prompted(
    target: str,
    command: list[str],
    *,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.5,
) -> bool:
    if not is_claude_command(command):
        return False

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pane = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", target],
            capture_output=True,
            text=True,
        )
        pane_text = pane.stdout.lower() if pane.returncode == 0 else ""
        if _looks_like_claude_theme_prompt(command, pane_text):
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "Enter"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(1.0)
            return True

        time.sleep(poll_interval_seconds)

    return False


def _looks_like_permission_bypass_prompt(command: list[str], pane_text: str) -> bool:
    if not pane_text or not is_claude_command(command):
        return False
    return (
        "bypass permissions mode" in pane_text
        and "yes, i accept" in pane_text
        and "no, exit" in pane_text
    )


def _looks_like_claude_theme_prompt(command: list[str], pane_text: str) -> bool:
    if not pane_text or not is_claude_command(command):
        return False
    return (
        "choose the text style that looks best with your terminal" in pane_text
        and "dark mode" in pane_text
        and "light mode" in pane_text
    )


def _looks_like_workspace_trust_prompt(command: list[str], pane_text: str) -> bool:
    if not pane_text:
        return False

    if is_claude_command(command):
        return ("trust this folder" in pane_text or "trust the contents" in pane_text) and (
            "enter to confirm" in pane_text or "press enter" in pane_text or "enter to continue" in pane_text
        )

    if is_codex_command(command):
        return (
            "trust the contents of this directory" in pane_text
            and "press enter to continue" in pane_text
        )

    if is_gemini_command(command):
        return "trust folder" in pane_text or "trust parent folder" in pane_text

    return False


def _wait_for_claude_ready(
    target: str,
    *,
    timeout_seconds: float = 30.0,
    poll_interval: float = 1.0,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pane = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", target],
            capture_output=True,
            text=True,
        )
        if pane.returncode == 0:
            text = pane.stdout
            text_lower = text.lower()
            if (
                "browser didn't open? use the url below to sign in" in text_lower
                or "paste code here if prompted" in text_lower
                or "oauth/authorize" in text_lower
            ):
                return False
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            tail = lines[-10:] if len(lines) >= 10 else lines
            for line in tail:
                if line.startswith(("❯", ">", "›")):
                    return True
                if "Try " in line and "write a test" in line:
                    return True
        time.sleep(poll_interval)
    return False


def _paste_tmux_prompt(target: str, worker_name: str, prompt: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        prefix=f"branchclaw-prompt-{worker_name}-",
        encoding="utf-8",
    ) as handle:
        handle.write(prompt)
        tmp_path = handle.name

    buffer_name = f"branchclaw-prompt-{worker_name}"
    try:
        subprocess.run(
            ["tmux", "load-buffer", "-b", buffer_name, tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-b", buffer_name, "-t", target],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.5)
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.3)
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    finally:
        subprocess.run(
            ["tmux", "delete-buffer", "-b", buffer_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        Path(tmp_path).unlink(missing_ok=True)
