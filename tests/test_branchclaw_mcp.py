from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from branchclaw.cli.commands import app
from branchclaw.config import BranchClawConfig
from branchclaw.mcp_state import (
    create_worker_mcp_session,
    ensure_mcp_server,
    load_worker_mcp_session_from_token,
    revoke_worker_mcp_session,
)
from branchclaw.models import ProjectProfile, WorkerRuntime, WorkerStatus, now_iso
from branchclaw.service import BranchClawService
from branchclaw.storage import EventStore
from branchclaw.worker_tools import execute_worker_tool_sync


def _init_web_repo(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "scripts": {"dev": "vite", "build": "vite build"},
                "packageManager": "npm@10.0.0",
            }
        ),
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "src" / "main.ts").write_text("console.log('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _append_worker(service: BranchClawService, run_id: str, workspace_path: Path) -> None:
    worker = WorkerRuntime(
        worker_name="worker-a",
        run_id=run_id,
        stage_id="stage-1",
        workspace_path=str(workspace_path),
        branch=f"branchclaw/{run_id}/worker-a",
        base_ref="main",
        backend="tmux",
        pid=0,
        child_pid=0,
        supervisor_pid=0,
        tmux_target="",
        task="improve the frontend",
        heartbeat_at=now_iso(),
        last_heartbeat_at=now_iso(),
        started_at=now_iso(),
        status=WorkerStatus.running,
        mcp_enabled=True,
        mcp_server_url="http://127.0.0.1:8765",
        mcp_token_id="mcp-test",
    )
    service.store.append(run_id, "worker.started", {"worker": worker.model_dump(mode="json")})


def test_worker_mcp_session_can_be_revoked(tmp_path):
    session = create_worker_mcp_session(
        run_id="run-1",
        worker_name="worker-a",
        stage_id="stage-1",
        workspace_path=str(tmp_path),
        repo_root=str(tmp_path),
        project_profile=ProjectProfile.web,
        server_url="http://127.0.0.1:8765",
    )

    loaded = load_worker_mcp_session_from_token(session.token)
    assert loaded is not None
    assert loaded.worker_name == "worker-a"

    assert revoke_worker_mcp_session(session.token_id, run_id="run-1", reason="done") is True
    assert load_worker_mcp_session_from_token(session.token) is None


def test_execute_worker_tools_emit_events_and_update_projection(tmp_path, monkeypatch):
    repo = _init_web_repo(tmp_path)
    service = BranchClawService()
    projection = service.create_run(
        "mcp-demo",
        project_profile="web",
        spec_content="spec",
        rules_content="rules",
        repo=str(repo),
    )
    _append_worker(service, projection.run.id, repo)
    session = create_worker_mcp_session(
        run_id=projection.run.id,
        worker_name="worker-a",
        stage_id=projection.run.current_stage_id,
        workspace_path=str(repo),
        repo_root=str(repo),
        project_profile=ProjectProfile.web,
        task="ship the UI",
        server_url="http://127.0.0.1:8765",
    )

    detected = execute_worker_tool_sync(session, "project.detect", {"repo_root": "."})
    assert detected["runtime"] == "node"
    assert detected["frontend"] is True

    monkeypatch.setattr(
        "branchclaw.worker_tools.launch_tmux_service",
        lambda **_kwargs: {
            "target": "preview:app",
            "log_path": str(repo / ".preview.log"),
            "launch_command": "npm run dev",
        },
    )
    started = execute_worker_tool_sync(
        session,
        "service.start_tmux",
        {
            "command": ["npm", "run", "dev"],
            "log_path": ".preview.log",
        },
    )
    assert started["target"] == "preview:app"

    monkeypatch.setattr("branchclaw.worker_tools.wait_for_url", lambda *_args, **_kwargs: "http://127.0.0.1:4173")
    discovered = execute_worker_tool_sync(
        session,
        "service.discover_url",
        {"log_path": ".preview.log", "timeout_seconds": 1.0},
    )
    assert discovered["url"] == "http://127.0.0.1:4173"

    reported = execute_worker_tool_sync(
        session,
        "worker.report_result",
        {
            "status": "success",
            "preview_url": "http://127.0.0.1:4173",
            "changed_surface_summary": "Improved the hero layout.",
            "architecture_summary": "# Architecture Change Summary\n\n- Changed areas: `src`\n",
        },
    )
    assert reported["status"] == "success"
    assert reported["report_source"] == "agent"

    projection = service.get_run(projection.run.id, rebuild=True)
    worker = projection.workers["worker-a"]
    assert worker.report_source == "agent"
    assert worker.discovered_url == "http://127.0.0.1:4173"
    assert worker.active_service_target == "preview:app"
    assert worker.last_tool_name == "worker.report_result"
    assert worker.last_tool_status == "completed"
    assert worker.result is not None
    assert worker.result.preview_url == "http://127.0.0.1:4173"

    event_types = [event.event_type for event in EventStore().list_events(projection.run.id)]
    assert "worker.tool_called" in event_types
    assert "worker.tool_completed" in event_types


def test_branchclaw_cli_mcp_serve_requests_daemon(monkeypatch):
    runner = CliRunner()
    captured: dict[str, object] = {}

    class DummyDaemon:
        def ensure_mcp_server(self, *, data_dir: str, run_id: str = ""):
            captured.update({"data_dir": data_dir, "run_id": run_id})
            return {
                "base_url": "http://127.0.0.1:8765",
                "managedStatus": "running",
            }

    monkeypatch.setattr(
        "branchclaw.cli.commands.BranchClawDaemonClient.require_running",
        lambda: DummyDaemon(),
    )

    result = runner.invoke(app, ["mcp", "serve", "--host", "127.0.0.1", "--port", "9900"])

    assert result.exit_code == 0
    assert captured["data_dir"]


def test_ensure_mcp_server_reuses_matching_data_dir(monkeypatch, tmp_path):
    current_data_dir = tmp_path / "branchclaw"
    config = BranchClawConfig(mcp_host="127.0.0.1", mcp_port=8765, mcp_start_timeout=1.0)

    monkeypatch.setattr("branchclaw.mcp_state.load_config", lambda: config)
    monkeypatch.setattr("branchclaw.mcp_state.get_data_dir", lambda: current_data_dir)
    monkeypatch.setattr(
        "branchclaw.mcp_state.read_mcp_server_status",
        lambda: {
            "pid": 4321,
            "host": "127.0.0.1",
            "port": 8765,
            "base_url": "http://127.0.0.1:8765",
            "started_at": "2026-03-23T00:00:00+00:00",
            "data_dir": str(current_data_dir),
        },
    )
    monkeypatch.setattr("branchclaw.mcp_state._pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(
        "branchclaw.mcp_state._mcp_server_info",
        lambda base_url, timeout_seconds=1.5: {
            "ok": True,
            "server": "branchclaw-mcp",
            "data_dir": str(current_data_dir.resolve()),
            "pid": 4321,
        }
        if base_url == "http://127.0.0.1:8765"
        else {},
    )

    status = ensure_mcp_server()

    assert status["reused"] is True
    assert status["started"] is False
    assert status["base_url"] == "http://127.0.0.1:8765"
    assert status["data_dir"] == str(current_data_dir.resolve())


def test_ensure_mcp_server_uses_fresh_port_for_different_data_dir(monkeypatch, tmp_path):
    current_data_dir = tmp_path / "branchclaw-current"
    other_data_dir = tmp_path / "branchclaw-other"
    config = BranchClawConfig(mcp_host="127.0.0.1", mcp_port=8765, mcp_start_timeout=1.0)
    popen_calls: list[list[str]] = []

    monkeypatch.setattr("branchclaw.mcp_state.load_config", lambda: config)
    monkeypatch.setattr("branchclaw.mcp_state.get_data_dir", lambda: current_data_dir)
    monkeypatch.setattr("branchclaw.mcp_state.read_mcp_server_status", lambda: {})
    monkeypatch.setattr("branchclaw.mcp_state.port_in_use", lambda host, port: port == 8765)
    monkeypatch.setattr("branchclaw.mcp_state._find_available_port", lambda host: 9911)
    monkeypatch.setattr(
        "branchclaw.mcp_state._mcp_server_info",
        lambda base_url, timeout_seconds=1.5: {
            "ok": True,
            "server": "branchclaw-mcp",
            "data_dir": str(other_data_dir.resolve()),
            "pid": 1111,
        }
        if base_url == "http://127.0.0.1:8765"
        else {
            "ok": True,
            "server": "branchclaw-mcp",
            "data_dir": str(current_data_dir.resolve()),
            "pid": 9876,
        }
        if base_url == "http://127.0.0.1:9911"
        else {},
    )
    monkeypatch.setattr(
        "branchclaw.mcp_state.subprocess.Popen",
        lambda args, **kwargs: popen_calls.append(args) or SimpleNamespace(pid=9876, poll=lambda: None),
    )

    status = ensure_mcp_server()

    assert status["started"] is True
    assert status["reused"] is False
    assert status["base_url"] == "http://127.0.0.1:9911"
    assert popen_calls
    assert popen_calls[0][-1] == "9911"
