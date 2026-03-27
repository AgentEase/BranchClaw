from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from branchclaw.cli.commands import app
from branchclaw.models import (
    BatchRecord,
    BatchStatus,
    FeatureRecord,
    FeatureStatus,
    ValidationStatus,
    WorkerRuntime,
    WorkerStatus,
    now_iso,
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


def test_branchclaw_cli_run_planner_constraint_archive_flow(tmp_path):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    env = {
        "HOME": str(tmp_path),
        "BRANCHCLAW_DATA_DIR": str(tmp_path / ".branchclaw"),
    }

    create = runner.invoke(
        app,
        ["run", "create", "demo", "--repo", str(repo), "--spec", "spec text", "--rules", "rules text"],
        env=env,
    )
    assert create.exit_code == 0
    run_id = create.output.split("Created run ")[1].split(" ")[0]

    propose = runner.invoke(
        app,
        ["planner", "propose", run_id, "implement it", "--summary", "phase 1"],
        env=env,
    )
    assert propose.exit_code == 0
    gate_id = propose.output.split("gate ")[1].split(" ")[0]

    approve = runner.invoke(
        app,
        ["planner", "approve", run_id, gate_id, "--actor", "reviewer"],
        env=env,
    )
    assert approve.exit_code == 0
    assert "executing" in approve.output

    add_constraint = runner.invoke(
        app,
        ["constraint", "add", run_id, "No force pushes", "--author", "human"],
        env=env,
    )
    assert add_constraint.exit_code == 0

    blocked_archive = runner.invoke(
        app,
        ["archive", "create", run_id, "--label", "phase-1"],
        env=env,
    )
    assert blocked_archive.exit_code == 1

    replan = runner.invoke(
        app,
        ["planner", "propose", run_id, "updated plan", "--summary", "phase 2"],
        env=env,
    )
    assert replan.exit_code == 0
    replan_gate = replan.output.split("gate ")[1].split(" ")[0]

    approve_replan = runner.invoke(
        app,
        ["planner", "approve", run_id, replan_gate, "--actor", "reviewer"],
        env=env,
    )
    assert approve_replan.exit_code == 0

    archive = runner.invoke(
        app,
        ["archive", "create", run_id, "--label", "phase-1"],
        env=env,
    )
    assert archive.exit_code == 0
    archive_gate = archive.output.split("gate ")[1].split(" ")[0]

    approve_archive = runner.invoke(
        app,
        ["planner", "approve", run_id, archive_gate, "--actor", "reviewer"],
        env=env,
    )
    assert approve_archive.exit_code == 0
    assert "archived" in approve_archive.output

    export = runner.invoke(
        app,
        ["event", "export", run_id],
        env=env,
    )
    assert export.exit_code == 0
    assert "Exported" in export.output


def test_branchclaw_cli_can_import_clawteam_team(tmp_path):
    import os

    from clawteam.team.manager import TeamManager
    from clawteam.team.tasks import TaskStore

    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    legacy_dir = tmp_path / ".legacy"
    legacy_dir.mkdir()

    os.environ["CLAWTEAM_DATA_DIR"] = str(legacy_dir)
    TeamManager.create_team("legacy", leader_name="leader", leader_id="leader001")
    TaskStore("legacy").create("task", owner="leader")

    env = {
        "HOME": str(tmp_path),
        "BRANCHCLAW_DATA_DIR": str(tmp_path / ".branchclaw"),
        "CLAWTEAM_DATA_DIR": str(legacy_dir),
    }

    result = runner.invoke(
        app,
        ["run", "migrate-clawteam", "legacy", "--repo", str(repo)],
        env=env,
    )
    assert result.exit_code == 0
    assert "Imported into run" in result.output


def test_branchclaw_cli_operator_views_show_health_and_pending_approvals(tmp_path, monkeypatch, branchclaw_daemon):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    env = {
        "HOME": str(tmp_path),
        "BRANCHCLAW_DATA_DIR": str(tmp_path / ".branchclaw"),
        "BRANCHCLAW_SUPERVISOR_START_TIMEOUT": "5",
        "BRANCHCLAW_HEARTBEAT_INTERVAL": "0.1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    create = runner.invoke(
        app,
        ["run", "create", "demo", "--repo", str(repo), "--spec", "spec text", "--rules", "rules text"],
        env=env,
    )
    assert create.exit_code == 0
    run_id = create.output.split("Created run ")[1].split(" ")[0]

    propose = runner.invoke(
        app,
        ["planner", "propose", run_id, "implement it", "--summary", "phase 1"],
        env=env,
    )
    assert propose.exit_code == 0
    gate_id = propose.output.split("gate ")[1].split(" ")[0]

    approve = runner.invoke(
        app,
        ["planner", "approve", run_id, gate_id, "--actor", "reviewer"],
        env=env,
    )
    assert approve.exit_code == 0

    service = BranchClawService()
    worker = service.spawn_worker(
        run_id,
        "worker1",
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
        backend="subprocess",
        task="sleep for operator view coverage",
    )
    assert worker.status.value == "running"

    workers = runner.invoke(
        app,
        ["worker", "list", run_id],
        env=env,
    )
    assert workers.exit_code == 0
    assert "Workers for" in workers.output
    assert "worker1" in workers.output
    assert "Task" in workers.output

    stop = runner.invoke(
        app,
        ["worker", "stop", run_id, "worker1"],
        env=env,
    )
    assert stop.exit_code == 0

    archive = runner.invoke(
        app,
        ["archive", "create", run_id, "--label", "phase-1"],
        env=env,
    )
    assert archive.exit_code == 0

    run_show = runner.invoke(
        app,
        ["run", "show", run_id],
        env=env,
    )
    assert run_show.exit_code == 0
    assert "Pending Approvals" in run_show.output
    assert "Interventions" in run_show.output
    assert "Working Tree Track" in run_show.output
    assert "Archives" in run_show.output
    assert "Constraints" in run_show.output

    board_show = runner.invoke(
        app,
        ["board", "show", run_id],
        env=env,
    )
    assert board_show.exit_code == 0
    assert "Pending Approvals" in board_show.output
    assert "Interventions" in board_show.output
    assert "Archives" in board_show.output
    assert "Workers" in board_show.output


def test_branchclaw_cli_worker_restart_command(tmp_path, monkeypatch):
    runner = CliRunner()
    env = {
        "HOME": str(tmp_path),
        "BRANCHCLAW_DATA_DIR": str(tmp_path / ".branchclaw"),
    }

    monkeypatch.setattr(
        "branchclaw.cli.commands.service.restart_worker",
        lambda run_id, worker_name: WorkerRuntime(
            worker_name=worker_name,
            run_id=run_id,
            stage_id="stage-1",
            workspace_path=str(tmp_path / "workspace"),
            branch=f"branchclaw/{run_id}/{worker_name}",
            base_ref="main",
            backend="tmux",
            pid=0,
            child_pid=0,
            supervisor_pid=123,
            tmux_target="branchclaw-demo:worker-a",
            task="resume work",
            heartbeat_at=now_iso(),
            last_heartbeat_at=now_iso(),
            started_at=now_iso(),
            status=WorkerStatus.running,
        ),
    )

    restarted = runner.invoke(app, ["worker", "restart", "run-123", "worker-a"], env=env)

    assert restarted.exit_code == 0
    assert "Restarted" in restarted.output
    assert "worker-a" in restarted.output


def test_branchclaw_cli_project_profile_and_worker_report_surface(tmp_path):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    env = {
        "HOME": str(tmp_path),
        "BRANCHCLAW_DATA_DIR": str(tmp_path / ".branchclaw"),
    }

    create = runner.invoke(
        app,
        [
            "run",
            "create",
            "web-demo",
            "--project-profile",
            "web",
            "--repo",
            str(repo),
            "--spec",
            "spec text",
            "--rules",
            "rules text",
        ],
        env=env,
    )
    assert create.exit_code == 0
    run_id = create.output.split("Created run ")[1].split(" ")[0]

    service = BranchClawService()
    projection = service.get_run(run_id)
    worker = WorkerRuntime(
        worker_name="worker-ui",
        run_id=run_id,
        stage_id=projection.run.current_stage_id,
        workspace_path=str(tmp_path / "workspace"),
        branch=f"branchclaw/{run_id}/worker-ui",
        base_ref="main",
        backend="tmux",
        pid=0,
        child_pid=0,
        supervisor_pid=0,
        tmux_target="",
        task="build preview",
        heartbeat_at=now_iso(),
        last_heartbeat_at=now_iso(),
        started_at=now_iso(),
        status=WorkerStatus.stopped,
    )
    service.store.append(
        run_id,
        "worker.started",
        {"worker": worker.model_dump(mode="json")},
    )

    report = runner.invoke(
        app,
        [
            "worker",
            "report",
            run_id,
            "worker-ui",
            "--status",
            "success",
            "--stack",
            "node",
            "--runtime",
            "node",
            "--package-manager",
            "npm",
            "--install-command",
            "npm install",
            "--start-command",
            "npm run dev",
            "--preview-url",
            "http://127.0.0.1:3000",
            "--changed-surface-summary",
            "Updated the landing page hero.",
            "--architecture-summary",
            "# Architecture Change Summary\n\n- Changed areas: `pages`\n",
            "--source",
            "fallback",
        ],
        env=env,
    )
    assert report.exit_code == 0

    workers = runner.invoke(
        app,
        ["worker", "list", run_id],
        env=env,
    )
    assert workers.exit_code == 0
    assert "ProjectProfile=web" in workers.output
    assert "worker-ui" in workers.output
    assert "http://127.0.0.1:3000" in workers.output

    run_show = runner.invoke(
        app,
        ["run", "show", run_id],
        env=env,
    )
    assert run_show.exit_code == 0
    assert "Project Profile" in run_show.output
    assert "Worker Reports" in run_show.output
    assert "Source: fallback" in run_show.output
    assert "Working Tree Track" in run_show.output
    assert "acceptance=success" in run_show.output
    assert "Updated the landing page hero." in run_show.output


def test_branchclaw_cli_run_create_accepts_multiline_spec_and_rules(tmp_path):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    env = {
        "HOME": str(tmp_path),
        "BRANCHCLAW_DATA_DIR": str(tmp_path / ".branchclaw"),
    }

    spec = "Line one of spec.\nLine two of spec.\nLine three of spec."
    rules = "- Keep it working.\n- Report blockers.\n- Preserve the UI."

    create = runner.invoke(
        app,
        [
            "run",
            "create",
            "multiline-demo",
            "--repo",
            str(repo),
            "--project-profile",
            "web",
            "--spec",
            spec,
            "--rules",
            rules,
        ],
        env=env,
    )

    assert create.exit_code == 0
    run_id = create.output.split("Created run ")[1].split(" ")[0]
    projection = BranchClawService().get_run(run_id)
    assert projection.run.project_profile.value == "web"
    assert projection.run.spec_content == spec
    assert projection.run.rules_content == rules


def test_branchclaw_cli_feature_and_batch_views(tmp_path):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    env = {
        "HOME": str(tmp_path),
        "BRANCHCLAW_DATA_DIR": str(tmp_path / ".branchclaw"),
    }

    create = runner.invoke(
        app,
        [
            "run",
            "create",
            "feature-views",
            "--repo",
            str(repo),
            "--spec",
            "spec text",
            "--rules",
            "rules text",
            "--direction",
            "Ship a backlog of focused improvements.",
            "--integration-ref",
            "branchclaw/feature-views/integration",
            "--max-active-features",
            "3",
        ],
        env=env,
    )
    assert create.exit_code == 0
    run_id = create.output.split("Created run ")[1].split(" ")[0]

    service = BranchClawService()
    projection = service.get_run(run_id)
    feature = FeatureRecord(
        id="feature-hero",
        run_id=run_id,
        stage_id=projection.run.current_stage_id,
        title="Hero Polish",
        goal="Improve the hero section.",
        task="Polish hero copy and spacing.",
        status=FeatureStatus.ready,
        claimed_areas=["ui"],
        priority=10,
        validation_status=ValidationStatus.passed,
        result_summary="Hero worktree is ready.",
    )
    batch = BatchRecord(
        id="batch-hero",
        run_id=run_id,
        stage_id=projection.run.current_stage_id,
        feature_ids=[feature.id],
        status=BatchStatus.pending_promote,
        integration_ref="branchclaw/feature-views/integration",
        validation_status=ValidationStatus.passed,
    )
    service.store.append(run_id, "feature.created", {"feature": feature.model_dump(mode="json")})
    service.store.append(run_id, "feature.ready", {"feature": feature.model_dump(mode="json")})
    service.store.append(run_id, "batch.proposed", {"batch": batch.model_dump(mode="json")})
    service.store.append(
        run_id,
        "batch.integration_validated",
        {"batch_id": batch.id, "validation_command": "npm run build", "validation_output": "build passed"},
    )

    feature_list = runner.invoke(app, ["feature", "list", run_id], env=env)
    assert feature_list.exit_code == 0
    assert "Hero Polish" in feature_list.output
    assert "batched" in feature_list.output

    batch_list = runner.invoke(app, ["batch", "list", run_id], env=env)
    assert batch_list.exit_code == 0
    assert "batch-hero" in batch_list.output
    assert "pending_promote" in batch_list.output

    run_show = runner.invoke(app, ["run", "show", run_id], env=env)
    assert run_show.exit_code == 0
    assert "Direction" in run_show.output
    assert "Integration Ref" in run_show.output
    assert "Feature Queue" in run_show.output
    assert "Batch Review" in run_show.output
