from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from typer.testing import CliRunner

from branchclaw.cli.commands import app
from branchclaw.storage import EventStore


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


def _env(tmp_path: Path) -> dict[str, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        _, board_port = sock.getsockname()
    return {
        "HOME": str(tmp_path),
        "BRANCHCLAW_DATA_DIR": str(tmp_path / ".branchclaw"),
        "BRANCHCLAW_DAEMON_ROOT": str(tmp_path / ".branchclawd"),
        "BRANCHCLAW_HEARTBEAT_INTERVAL": "0.1",
        "BRANCHCLAW_SUPERVISOR_START_TIMEOUT": "5",
        "BRANCHCLAW_BOARD_HOST": "127.0.0.1",
        "BRANCHCLAW_BOARD_PORT": str(board_port),
    }


def test_branchclaw_daemon_cli_start_status_stop(tmp_path):
    runner = CliRunner()
    env = _env(tmp_path)

    started = runner.invoke(app, ["daemon", "start"], env=env)
    assert started.exit_code == 0

    status = runner.invoke(app, ["--json", "daemon", "status"], env=env)
    assert status.exit_code == 0
    payload = json.loads(status.stdout)
    assert payload["running"] is True
    assert payload["daemon_pid"] > 0
    assert payload["dashboard_running"] is True
    assert payload["dashboard_url"].startswith("http://")
    assert payload["dashboard_port"] > 0

    stopped = runner.invoke(app, ["daemon", "stop"], env=env)
    assert stopped.exit_code == 0

    status_after = runner.invoke(app, ["--json", "daemon", "status"], env=env)
    assert status_after.exit_code == 0
    payload_after = json.loads(status_after.stdout)
    assert payload_after["running"] is False


def test_daemon_start_recovers_orphaned_saved_process(tmp_path):
    runner = CliRunner()
    env = _env(tmp_path)
    daemon_root = Path(env["BRANCHCLAW_DAEMON_ROOT"])
    daemon_root.mkdir(parents=True, exist_ok=True)
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "branchclaw", "daemon", "serve"]
    )
    try:
        (daemon_root / "state.json").write_text(
            json.dumps(
                {
                    "running": True,
                    "daemon_pid": orphan.pid,
                    "socket_path": str(daemon_root / "daemon.sock"),
                    "started_at": "2026-03-24T00:00:00+00:00",
                    "dashboard_running": True,
                    "dashboard_host": "127.0.0.1",
                    "dashboard_port": int(env["BRANCHCLAW_BOARD_PORT"]),
                    "dashboard_url": f"http://127.0.0.1:{env['BRANCHCLAW_BOARD_PORT']}",
                    "data_dirs": [],
                    "processes": [],
                }
            ),
            encoding="utf-8",
        )

        started = runner.invoke(app, ["daemon", "start"], env=env)
        assert started.exit_code == 0

        deadline = time.time() + 3.0
        while time.time() < deadline and orphan.poll() is None:
            time.sleep(0.1)
        assert orphan.poll() is not None

        status = runner.invoke(app, ["--json", "daemon", "status"], env=env)
        assert status.exit_code == 0
        payload = json.loads(status.stdout)
        assert payload["running"] is True
        assert payload["daemon_pid"] != orphan.pid
    finally:
        if orphan.poll() is None:
            orphan.terminate()
            orphan.wait(timeout=5)
        runner.invoke(app, ["daemon", "stop"], env=env)


def test_worker_spawn_requires_running_daemon(tmp_path):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    env = _env(tmp_path)

    created = runner.invoke(
        app,
        ["--json", "run", "create", "demo", "--repo", str(repo), "--spec", "spec", "--rules", "rules"],
        env=env,
    )
    run_id = json.loads(created.stdout)["id"]
    proposed = runner.invoke(
        app,
        ["--json", "planner", "propose", run_id, "ship it", "--summary", "phase 1"],
        env=env,
    )
    gate_id = json.loads(proposed.stdout)["gateId"]
    approved = runner.invoke(
        app,
        ["planner", "approve", run_id, gate_id, "--actor", "reviewer"],
        env=env,
    )
    assert approved.exit_code == 0

    spawn = runner.invoke(
        app,
        [
            "--json",
            "worker",
            "spawn",
            run_id,
            "worker-a",
            "--backend",
            "subprocess",
            "--task",
            "sleep",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ],
        env=env,
    )
    assert spawn.exit_code == 1
    assert "daemon is not running" in spawn.stdout.lower()


def test_daemon_manages_worker_board_and_mcp_services(tmp_path):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    env = _env(tmp_path)

    assert runner.invoke(app, ["daemon", "start"], env=env).exit_code == 0
    created = runner.invoke(
        app,
        ["--json", "run", "create", "demo", "--repo", str(repo), "--spec", "spec", "--rules", "rules"],
        env=env,
    )
    run_id = json.loads(created.stdout)["id"]
    proposed = runner.invoke(
        app,
        ["--json", "planner", "propose", run_id, "ship it", "--summary", "phase 1"],
        env=env,
    )
    gate_id = json.loads(proposed.stdout)["gateId"]
    assert runner.invoke(app, ["planner", "approve", run_id, gate_id, "--actor", "reviewer"], env=env).exit_code == 0

    spawned = runner.invoke(
        app,
        [
            "--json",
            "worker",
            "spawn",
            run_id,
            "worker-a",
            "--backend",
            "subprocess",
            "--task",
            "sleep",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ],
        env=env,
    )
    assert spawned.exit_code == 0
    worker = json.loads(spawned.stdout)
    assert worker["managed_by_daemon"] is True
    assert worker["daemon_pid"] > 0

    board = runner.invoke(app, ["--json", "board", "serve"], env=env)
    assert board.exit_code == 0
    board_payload = json.loads(board.stdout)
    assert board_payload["dashboard_url"].startswith("http://")
    with urllib.request.urlopen(board_payload["dashboard_url"], timeout=5) as response:
        assert response.status == 200
    assert runner.invoke(app, ["mcp", "serve"], env=env).exit_code == 0

    ps = runner.invoke(app, ["--json", "daemon", "ps"], env=env)
    assert ps.exit_code == 0
    daemon_state = json.loads(ps.stdout)
    assert daemon_state["dashboard_running"] is True
    processes = daemon_state["processes"]
    kinds = {item["process_kind"] for item in processes}
    assert {"supervisor", "mcp"} <= kinds
    assert "board" not in kinds

    status = runner.invoke(app, ["--json", "daemon", "status"], env=env)
    assert status.exit_code == 0
    status_payload = json.loads(status.stdout)
    dashboard_url = status_payload["dashboard_url"]
    daemon_payload = json.loads(urllib.request.urlopen(f"{dashboard_url}/api/daemon/status", timeout=5).read().decode("utf-8"))
    assert daemon_payload["running"] is True
    assert daemon_payload["dashboard_url"] == dashboard_url
    runs_payload = json.loads(urllib.request.urlopen(f"{dashboard_url}/api/runs", timeout=5).read().decode("utf-8"))
    assert runs_payload[0]["id"] == run_id
    assert runs_payload[0]["dataDirKey"]
    assert runs_payload[0]["ownerDataDir"] == env["BRANCHCLAW_DATA_DIR"]
    data_dir_key = runs_payload[0]["dataDirKey"]

    reconcile_request = urllib.request.Request(
        f"{dashboard_url}/api/data-dirs/{data_dir_key}/runs/{run_id}/reconcile",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    reconcile_payload = json.loads(urllib.request.urlopen(reconcile_request, timeout=5).read().decode("utf-8"))
    assert reconcile_payload["runId"] == run_id

    stop_request = urllib.request.Request(
        f"{dashboard_url}/api/data-dirs/{data_dir_key}/runs/{run_id}/workers/worker-a/stop",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    stop_payload = json.loads(urllib.request.urlopen(stop_request, timeout=5).read().decode("utf-8"))
    assert stop_payload["requested"] is True
    assert stop_payload["status"] == "stopped"

    deadline = time.time() + 5.0
    worker_runtime = None
    while time.time() < deadline:
        listed = runner.invoke(app, ["--json", "worker", "list", run_id], env=env)
        assert listed.exit_code == 0
        worker_runtime = json.loads(listed.stdout)[0]
        if worker_runtime["status"] == "stopped":
            break
        time.sleep(0.1)
    assert worker_runtime is not None
    assert worker_runtime["status"] == "stopped"

    restart_request = urllib.request.Request(
        f"{dashboard_url}/api/data-dirs/{data_dir_key}/runs/{run_id}/workers/worker-a/restart",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    restart_payload = json.loads(urllib.request.urlopen(restart_request, timeout=5).read().decode("utf-8"))
    assert restart_payload["requested"] is True
    assert restart_payload["workerName"] == "worker-a"

    assert runner.invoke(app, ["board", "stop"], env=env).exit_code == 0
    assert runner.invoke(app, ["mcp", "stop"], env=env).exit_code == 0
    assert runner.invoke(app, ["daemon", "stop"], env=env).exit_code == 0


def test_daemon_dashboard_can_create_run_and_workspace_in_attached_data_dir(tmp_path):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    env = _env(tmp_path)

    assert runner.invoke(app, ["daemon", "start"], env=env).exit_code == 0
    board = runner.invoke(app, ["--json", "board", "serve"], env=env)
    assert board.exit_code == 0
    dashboard_url = json.loads(board.stdout)["dashboard_url"]

    target_data_dir = tmp_path / "sessions" / "alpha" / ".branchclaw"
    create_request = urllib.request.Request(
        f"{dashboard_url}/api/runs",
        data=json.dumps(
            {
                "dataDir": str(target_data_dir),
                "repo": str(repo),
                "name": "dashboard-run",
                "description": "created from dashboard",
                "direction": "Keep a long-lived backlog for the repo.",
                "integrationRef": "branchclaw/dashboard-run/integration",
                "maxActiveFeatures": 3,
                "projectProfile": "web",
                "specContent": "spec",
                "rulesContent": "rules",
                "initialPlan": "launch the first manual dashboard flow",
                "author": "dashboard",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(create_request, timeout=5) as response:
        created_body = json.loads(response.read().decode("utf-8"))

    assert created_body["dataDirKey"]
    assert created_body["runId"].startswith("dashboard-run-")
    assert created_body["gateId"].startswith("gate-")
    assert created_body["runStatus"] == "awaiting_plan_approval"

    runs_payload = json.loads(
        urllib.request.urlopen(f"{dashboard_url}/api/runs", timeout=5).read().decode("utf-8")
    )
    created_run = next(item for item in runs_payload if item["id"] == created_body["runId"])
    assert created_run["ownerDataDir"] == str(target_data_dir.resolve())
    assert created_run["status"] == "awaiting_plan_approval"
    assert created_run["direction"] == "Keep a long-lived backlog for the repo."
    assert created_run["integrationRef"] == "branchclaw/dashboard-run/integration"
    assert created_run["maxActiveFeatures"] == 3

    spawn_request = urllib.request.Request(
        f"{dashboard_url}/api/data-dirs/{created_body['dataDirKey']}/runs/{created_body['runId']}/workers",
        data=json.dumps(
            {
                "featureId": "feature-demo",
                "workerName": "worker-ui",
                "task": "sleep for dashboard workspace test",
                "backend": "subprocess",
                "command": f"{sys.executable} -c \"import time; time.sleep(60)\"",
                "skipPermissions": False,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(spawn_request, timeout=5) as response:
        spawn_body = json.loads(response.read().decode("utf-8"))

    assert spawn_body["requested"] is True
    assert spawn_body["workerName"] == "worker-ui"
    assert "/stage-1/worker-ui" in spawn_body["workspacePath"]

    run_payload = json.loads(
        urllib.request.urlopen(
            f"{dashboard_url}/api/data-dirs/{created_body['dataDirKey']}/runs/{created_body['runId']}",
            timeout=5,
        ).read().decode("utf-8")
    )
    worker = next(item for item in run_payload["workers"] if item["worker_name"] == "worker-ui")
    assert worker["workspace_path"].endswith("/stage-1/worker-ui")
    assert worker["feature_id"] == "feature-demo"

    assert runner.invoke(app, ["daemon", "stop"], env=env).exit_code == 0


def test_daemon_retains_historical_data_dirs_across_restart(tmp_path):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    env = _env(tmp_path)

    assert runner.invoke(app, ["daemon", "start"], env=env).exit_code == 0
    created = runner.invoke(
        app,
        ["--json", "run", "create", "history-demo", "--repo", str(repo), "--spec", "spec", "--rules", "rules"],
        env=env,
    )
    run_id = json.loads(created.stdout)["id"]

    assert runner.invoke(app, ["mcp", "serve"], env=env).exit_code == 0
    first_status = runner.invoke(app, ["--json", "daemon", "status"], env=env)
    assert first_status.exit_code == 0
    first_payload = json.loads(first_status.stdout)
    assert first_payload["data_dirs"]
    remembered_dirs = {item["data_dir"] for item in first_payload["data_dirs"]}
    remembered_dir = env["BRANCHCLAW_DATA_DIR"]
    assert remembered_dir in remembered_dirs

    assert runner.invoke(app, ["daemon", "stop"], env=env).exit_code == 0
    stopped_status = runner.invoke(app, ["--json", "daemon", "status"], env=env)
    assert stopped_status.exit_code == 0
    stopped_payload = json.loads(stopped_status.stdout)
    assert stopped_payload["running"] is False
    assert stopped_payload["data_dirs"]
    assert remembered_dir in {item["data_dir"] for item in stopped_payload["data_dirs"]}

    assert runner.invoke(app, ["daemon", "start"], env=env).exit_code == 0
    board = runner.invoke(app, ["--json", "board", "serve"], env=env)
    assert board.exit_code == 0
    dashboard_url = json.loads(board.stdout)["dashboard_url"]

    runs_payload = json.loads(
        urllib.request.urlopen(f"{dashboard_url}/api/runs", timeout=5).read().decode("utf-8")
    )
    remembered_run = next(item for item in runs_payload if item["id"] == run_id)
    assert remembered_run["ownerDataDir"] == remembered_dir

    assert runner.invoke(app, ["daemon", "stop"], env=env).exit_code == 0


def test_daemon_discovers_historical_artifact_data_dirs(tmp_path, monkeypatch):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    historical_data_dir = tmp_path / "artifacts" / "session-a" / ".branchclaw"
    historical_data_dir.mkdir(parents=True)
    creation_env = _env(tmp_path)
    creation_env["BRANCHCLAW_DATA_DIR"] = str(historical_data_dir)

    created = runner.invoke(
        app,
        ["--json", "run", "create", "artifact-history", "--repo", str(repo), "--spec", "spec", "--rules", "rules"],
        env=creation_env,
    )
    assert created.exit_code == 0
    run_id = json.loads(created.stdout)["id"]

    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["daemon", "start"], env=env).exit_code == 0

    board = runner.invoke(app, ["--json", "board", "serve"], env=env)
    assert board.exit_code == 0
    dashboard_url = json.loads(board.stdout)["dashboard_url"]
    runs_payload = json.loads(
        urllib.request.urlopen(f"{dashboard_url}/api/runs", timeout=5).read().decode("utf-8")
    )
    assert runs_payload
    assert runs_payload[0]["id"] == run_id
    assert runs_payload[0]["ownerDataDir"] == str(historical_data_dir.resolve())

    assert runner.invoke(app, ["daemon", "stop"], env=env).exit_code == 0


def test_worker_reconcile_via_daemon_blocks_retry_exhausted_worker(tmp_path):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    env = _env(tmp_path)
    env["BRANCHCLAW_WORKER_TOOL_RETRY_LIMIT"] = "2"
    env["BRANCHCLAW_WORKER_AUTO_REMEDIATION_LIMIT"] = "0"

    assert runner.invoke(app, ["daemon", "start"], env=env).exit_code == 0
    created = runner.invoke(
        app,
        ["--json", "run", "create", "demo", "--repo", str(repo), "--spec", "spec", "--rules", "rules"],
        env=env,
    )
    run_id = json.loads(created.stdout)["id"]
    proposed = runner.invoke(
        app,
        ["--json", "planner", "propose", run_id, "ship it", "--summary", "phase 1"],
        env=env,
    )
    gate_id = json.loads(proposed.stdout)["gateId"]
    assert runner.invoke(app, ["planner", "approve", run_id, gate_id, "--actor", "reviewer"], env=env).exit_code == 0

    spawned = runner.invoke(
        app,
        [
            "--json",
            "worker",
            "spawn",
            run_id,
            "worker-a",
            "--backend",
            "subprocess",
            "--task",
            "sleep",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ],
        env=env,
    )
    assert spawned.exit_code == 0

    for _ in range(3):
        EventStore().append(
            run_id,
            "worker.tool_failed",
            {
                "worker_name": "worker-a",
                "tool_name": "project.install_dependencies",
                "error": "corepack missing",
                "diff_signature": "",
            },
        )

    reconciled = runner.invoke(app, ["worker", "reconcile", run_id], env=env)
    assert reconciled.exit_code == 0

    deadline = time.time() + 3.0
    runtime = None
    while time.time() < deadline:
        listed = runner.invoke(app, ["--json", "worker", "list", run_id], env=env)
        runtime = json.loads(listed.stdout)[0]
        if runtime["status"] == "blocked":
            break
        time.sleep(0.1)

    assert runtime is not None
    assert runtime["status"] == "blocked"
    assert "failed 3 times" in runtime["blocked_reason"]
    assert runtime["lastToolStatus"] == "failed"

    assert runner.invoke(app, ["daemon", "stop"], env=env).exit_code == 0
