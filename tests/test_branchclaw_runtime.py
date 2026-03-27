from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from branchclaw.config import load_config
from branchclaw.models import WorkerMcpSession, WorkerRuntime, WorkerStatus, now_iso
from branchclaw.runtime import (
    _confirm_claude_theme_if_prompted,
    _confirm_permission_bypass_if_prompted,
    _launch_child,
    _wait_for_claude_ready,
    worker_launch_path,
)
from branchclaw.service import BranchClawService


def _init_git_repo(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_branchclaw_config_reads_skip_permissions_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BRANCHCLAW_SKIP_PERMISSIONS", "1")
    monkeypatch.setenv("BRANCHCLAW_CLAUDE_READY_TIMEOUT", "42")
    monkeypatch.setenv("BRANCHCLAW_WORKER_BLOCK_AFTER", "120")
    monkeypatch.setenv("BRANCHCLAW_WORKER_TOOL_RETRY_LIMIT", "5")
    monkeypatch.setenv("BRANCHCLAW_WORKER_AUTO_REMEDIATION_LIMIT", "4")
    monkeypatch.setenv("BRANCHCLAW_WORKER_AUTO_RESTART_LIMIT", "2")
    monkeypatch.setenv("BRANCHCLAW_DAEMON_WATCHDOG_INTERVAL", "2")

    config = load_config()

    assert config.skip_permissions is True
    assert config.claude_ready_timeout == 42.0
    assert config.worker_block_after == 120.0
    assert config.worker_tool_retry_limit == 5
    assert config.worker_auto_remediation_limit == 4
    assert config.worker_auto_restart_limit == 2
    assert config.daemon_watchdog_interval == 2.0


def test_spawn_worker_persists_skip_permissions_in_launch_payload(monkeypatch, tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("demo", spec_content="spec", rules_content="rules", repo=str(repo))
    _, gate = service.propose_plan(projection.run.id, "do work", summary="phase 1")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")

    class DummyDaemon:
        def ensure_mcp_server(self, **_kwargs):
            return {
                "base_url": "http://127.0.0.1:8765",
                "pid": 9001,
                "reused": True,
                "daemonPid": 7001,
            }

        def launch_supervisor(self, **_kwargs):
            return {"supervisorPid": 4242, "daemonPid": 7001}

    monkeypatch.setattr(
        "branchclaw.service.BranchClawDaemonClient.require_running",
        lambda: DummyDaemon(),
    )
    monkeypatch.setattr(
        "branchclaw.service.create_worker_mcp_session",
        lambda **kwargs: WorkerMcpSession(
            token_id="mcp-demo",
            token="mcp-demo.secret",
            run_id=kwargs["run_id"],
            worker_name=kwargs["worker_name"],
            stage_id=kwargs["stage_id"],
            workspace_path=kwargs["workspace_path"],
            repo_root=kwargs["repo_root"],
            project_profile=kwargs["project_profile"],
            task=kwargs["task"],
            server_url=kwargs["server_url"],
            allowed_tools=["context.get_worker_context"],
        ),
    )

    captured: dict[str, object] = {}

    def fake_await(self, run_id: str, worker_name: str, supervisor_pid: int):
        payload = json.loads(worker_launch_path(run_id, worker_name).read_text(encoding="utf-8"))
        captured.update(payload)
        return WorkerRuntime(
            worker_name=worker_name,
            run_id=run_id,
            stage_id=payload["stage_id"],
            workspace_path=payload["workspace_path"],
            branch=payload["branch"],
            base_ref=payload["base_ref"],
            backend=payload["backend"],
            pid=0,
            child_pid=0,
            supervisor_pid=supervisor_pid,
            tmux_target="branchclaw-demo:worker-a",
            task=payload["task"],
            heartbeat_at=now_iso(),
            last_heartbeat_at=now_iso(),
            started_at=now_iso(),
            status=WorkerStatus.running,
        )

    monkeypatch.setattr(BranchClawService, "_await_worker_start", fake_await)

    worker = service.spawn_worker(
        projection.run.id,
        "worker-a",
        command=["claude"],
        backend="tmux",
        task="do work",
        skip_permissions=True,
    )

    assert worker.supervisor_pid == 4242
    assert captured["command"] == ["claude"]
    assert captured["skip_permissions"] is True
    assert captured["mcp_enabled"] is True
    assert captured["mcp_server_url"] == "http://127.0.0.1:8765"
    assert captured["mcp_token_id"] == "mcp-demo"


def test_spawn_worker_injects_project_profile_skill_prompt(monkeypatch, tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run(
        "web-demo",
        project_profile="web",
        spec_content="spec",
        rules_content="rules",
        repo=str(repo),
    )
    _, gate = service.propose_plan(projection.run.id, "do work", summary="phase 1")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")

    class DummyDaemon:
        def ensure_mcp_server(self, **_kwargs):
            return {
                "base_url": "http://127.0.0.1:8765",
                "pid": 9001,
                "reused": True,
                "daemonPid": 7001,
            }

        def launch_supervisor(self, **_kwargs):
            return {"supervisorPid": 5151, "daemonPid": 7001}

    monkeypatch.setattr(
        "branchclaw.service.BranchClawDaemonClient.require_running",
        lambda: DummyDaemon(),
    )
    monkeypatch.setattr(
        "branchclaw.service.create_worker_mcp_session",
        lambda **kwargs: WorkerMcpSession(
            token_id="mcp-web",
            token="mcp-web.secret",
            run_id=kwargs["run_id"],
            worker_name=kwargs["worker_name"],
            stage_id=kwargs["stage_id"],
            workspace_path=kwargs["workspace_path"],
            repo_root=kwargs["repo_root"],
            project_profile=kwargs["project_profile"],
            task=kwargs["task"],
            server_url=kwargs["server_url"],
            allowed_tools=["context.get_worker_context"],
        ),
    )

    captured: dict[str, object] = {}

    def fake_await(self, run_id: str, worker_name: str, supervisor_pid: int):
        payload = json.loads(worker_launch_path(run_id, worker_name).read_text(encoding="utf-8"))
        captured.update(payload)
        return WorkerRuntime(
            worker_name=worker_name,
            run_id=run_id,
            stage_id=payload["stage_id"],
            workspace_path=payload["workspace_path"],
            branch=payload["branch"],
            base_ref=payload["base_ref"],
            backend=payload["backend"],
            pid=0,
            child_pid=0,
            supervisor_pid=supervisor_pid,
            tmux_target="branchclaw-web-demo:worker-a",
            task=payload["task"],
            heartbeat_at=now_iso(),
            last_heartbeat_at=now_iso(),
            started_at=now_iso(),
            status=WorkerStatus.running,
        )

    monkeypatch.setattr(BranchClawService, "_await_worker_start", fake_await)

    worker = service.spawn_worker(
        projection.run.id,
        "worker-a",
        command=["claude"],
        backend="tmux",
        task="ship the frontend change",
        skip_permissions=True,
    )

    assert worker.supervisor_pid == 5151
    assert captured["env"]["BRANCHCLAW_PROJECT_PROFILE"] == "web"
    assert "Project-Type Development Pack" in captured["prompt"]
    assert "branch-agent-project-dev" in captured["prompt"]
    assert "branch-agent-web-dev" in captured["prompt"]
    assert "report_result.py" in captured["prompt"]
    assert "BranchClaw MCP tools" in captured["prompt"]
    assert "context.get_worker_context" in captured["prompt"]
    assert "Do not `cd` into the BranchClaw repository" in captured["prompt"]
    assert "--repo-root ." in captured["prompt"]
    assert "native MCP tools" in captured["system_prompt"]
    assert "Do not run invented shell commands such as `mcp call ...`." in captured["system_prompt"]


def test_launch_child_tmux_uses_skip_permissions_and_trust_ready_flow(monkeypatch, tmp_path):
    run_calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        run_calls.append(args)
        if args[:3] == ["tmux", "has-session", "-t"]:
            return Result(returncode=1)
        if args[:3] == ["tmux", "list-panes", "-t"] and "#{pane_id}" in args:
            return Result(returncode=0, stdout="%1\n")
        if args[:3] == ["tmux", "list-panes", "-t"] and "#{pane_pid}" in args:
            return Result(returncode=0, stdout="9876\n")
        return Result(returncode=0, stdout="")

    def fake_which(name, path=None):
        if name in {"tmux", "claude"}:
            return f"/usr/bin/{name}"
        return None

    trust_calls: list[tuple[str, list[str]]] = []
    ready_calls: list[tuple[str, float]] = []
    paste_calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr("branchclaw.runtime.shutil.which", fake_which)
    monkeypatch.setattr("branchclaw.runtime.subprocess.run", fake_run)
    monkeypatch.setattr(
        "branchclaw.runtime._confirm_workspace_trust_if_prompted",
        lambda target, command: trust_calls.append((target, command)) or True,
    )
    monkeypatch.setattr(
        "branchclaw.runtime._wait_for_claude_ready",
        lambda target, timeout_seconds=30.0, poll_interval=1.0: ready_calls.append((target, timeout_seconds)) or True,
    )
    monkeypatch.setattr(
        "branchclaw.runtime._paste_tmux_prompt",
        lambda target, worker_name, prompt: paste_calls.append((target, worker_name, prompt)),
    )
    monkeypatch.setattr("branchclaw.runtime.time.sleep", lambda *_args, **_kwargs: None)

    write_config_calls: list[tuple[str, str, str, str]] = []

    monkeypatch.setattr(
        "branchclaw.runtime.write_worker_mcp_config",
        lambda run_id, worker_name, *, server_url, token, server_name="branchclaw-worker": (
            write_config_calls.append((run_id, worker_name, server_url, token)) or (tmp_path / "mcp-config.json")
        ),
    )

    result = _launch_child(
        command=["claude"],
        backend="tmux",
        cwd=str(tmp_path),
        prompt="do the work",
        system_prompt="use native tools first",
        env={"PATH": "/usr/bin:/bin", "BRANCHCLAW_RUN_ID": "demo"},
        worker_name="worker-a",
        run_id="demo-run",
        skip_permissions=True,
        ready_timeout=12.0,
        mcp_enabled=True,
        mcp_server_url="http://127.0.0.1:8765",
        mcp_token="mcp-demo.secret",
    )

    assert result["tmux_target"] == "branchclaw-demo-run:worker-a"
    new_session = next(call for call in run_calls if call[:3] == ["tmux", "new-session", "-d"])
    assert "--dangerously-skip-permissions" in new_session[-1]
    assert "--mcp-config" in new_session[-1]
    assert "--strict-mcp-config" in new_session[-1]
    assert "--append-system-prompt" in new_session[-1]
    assert "use native tools first" in new_session[-1]
    assert trust_calls == [("branchclaw-demo-run:worker-a", ["claude"])]
    assert ready_calls == [("branchclaw-demo-run:worker-a", 12.0)]
    assert paste_calls == [("branchclaw-demo-run:worker-a", "worker-a", "do the work")]
    assert write_config_calls == [("demo-run", "worker-a", "http://127.0.0.1:8765", "mcp-demo.secret")]


def test_branchclaw_runtime_confirms_claude_bypass_permissions_prompt(monkeypatch):
    run_calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(args, **kwargs):
        run_calls.append(args)
        if args[:4] == ["tmux", "capture-pane", "-p", "-t"]:
            return Result(
                stdout=(
                    "WARNING: Claude Code running in Bypass Permissions mode\n"
                    "1. No, exit\n"
                    "2. Yes, I accept\n"
                )
            )
        return Result()

    monkeypatch.setattr("branchclaw.runtime.subprocess.run", fake_run)
    monkeypatch.setattr("branchclaw.runtime.time.sleep", lambda *_args, **_kwargs: None)

    confirmed = _confirm_permission_bypass_if_prompted("demo:worker", ["claude"])

    assert confirmed is True
    assert ["tmux", "send-keys", "-t", "demo:worker", "Down"] in run_calls
    assert ["tmux", "send-keys", "-t", "demo:worker", "Enter"] in run_calls


def test_branchclaw_runtime_confirms_claude_theme_prompt(monkeypatch):
    run_calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(args, **kwargs):
        run_calls.append(args)
        if args[:4] == ["tmux", "capture-pane", "-p", "-t"]:
            return Result(
                stdout=(
                    "Let's get started.\n"
                    "Choose the text style that looks best with your terminal\n"
                    "1. Dark mode\n"
                    "2. Light mode\n"
                )
            )
        return Result()

    monkeypatch.setattr("branchclaw.runtime.subprocess.run", fake_run)
    monkeypatch.setattr("branchclaw.runtime.time.sleep", lambda *_args, **_kwargs: None)

    confirmed = _confirm_claude_theme_if_prompted("demo:worker", ["claude"])

    assert confirmed is True
    assert ["tmux", "send-keys", "-t", "demo:worker", "Enter"] in run_calls


def test_await_worker_start_respects_claude_ready_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    launch_path = worker_launch_path("demo-run", "worker-a")
    launch_path.write_text(json.dumps({"claude_ready_timeout": 30}), encoding="utf-8")

    service = BranchClawService()
    worker = WorkerRuntime(
        worker_name="worker-a",
        run_id="demo-run",
        stage_id="stage-1",
        workspace_path="/tmp/demo",
        branch="branchclaw/demo-run/worker-a",
        base_ref="main",
        backend="tmux",
        pid=0,
        child_pid=0,
        supervisor_pid=1234,
        tmux_target="branchclaw-demo-run:worker-a",
        task="demo",
        heartbeat_at=now_iso(),
        last_heartbeat_at=now_iso(),
        started_at=now_iso(),
        status=WorkerStatus.running,
    )

    call_count = {"value": 0}

    def fake_get_run(self, _run_id: str, *, rebuild: bool = False):
        call_count["value"] += 1
        if call_count["value"] < 13:
            return SimpleNamespace(workers={})
        return SimpleNamespace(workers={"worker-a": worker})

    clock = {"value": 0.0}

    def fake_time():
        clock["value"] += 1.0
        return clock["value"]

    monkeypatch.setattr("branchclaw.service.read_worker_status", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("branchclaw.service.pid_alive", lambda _pid: True)
    monkeypatch.setattr("branchclaw.service.load_config", lambda: SimpleNamespace(supervisor_start_timeout=10.0))
    monkeypatch.setattr(BranchClawService, "get_run", fake_get_run)
    monkeypatch.setattr("branchclaw.service.time.time", fake_time)
    monkeypatch.setattr("branchclaw.service.time.sleep", lambda *_args, **_kwargs: None)

    started = service._await_worker_start("demo-run", "worker-a", 1234)

    assert started is worker


def test_wait_for_claude_ready_short_circuits_on_oauth_prompt(monkeypatch):
    class Result:
        def __init__(self, returncode: int = 0, stdout: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    monkeypatch.setattr(
        "branchclaw.runtime.subprocess.run",
        lambda *args, **kwargs: Result(
            stdout=(
                "Browser didn't open? Use the url below to sign in:\n"
                "https://claude.ai/oauth/authorize?code=true\n"
                "Paste code here if prompted >\n"
            )
        ),
    )
    monkeypatch.setattr("branchclaw.runtime.time.sleep", lambda *_args, **_kwargs: None)

    assert _wait_for_claude_ready("demo:worker", timeout_seconds=30.0) is False
