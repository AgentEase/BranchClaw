from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from branchclaw.board import summarize_run
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
from branchclaw.service import BranchClawError, BranchClawService
from branchclaw.storage import EventStore, artifacts_dir


def _init_git_repo(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _create_branch_commit(repo: Path, branch: str, filename: str, content: str) -> str:
    subprocess.run(["git", "checkout", "-b", branch], cwd=repo, check=True, capture_output=True)
    (repo / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", f"update {branch}"], cwd=repo, check=True, capture_output=True)
    sha = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    return sha


def test_projection_rebuild_roundtrip(tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run(
        "demo",
        description="demo run",
        spec_content="base spec",
        rules_content="shared rules",
        repo=str(repo),
    )
    run_id = projection.run.id
    _, gate = service.propose_plan(run_id, "Implement feature A", summary="phase 1", author="planner")
    service.approve_gate(run_id, gate.id, actor="reviewer", feedback="ship it")
    service.add_constraint(run_id, "Do not modify generated files", author="human")

    rebuilt = EventStore().load_projection(run_id, rebuild=True)

    assert rebuilt.run.status.value == "executing"
    assert rebuilt.run.project_profile.value == "backend"
    assert rebuilt.run.active_plan_id.startswith("plan-")
    assert rebuilt.stats.event_count >= 6
    assert rebuilt.constraints[0].content == "Do not modify generated files"
    assert rebuilt.run.needs_replan is True
    assert "Base Spec" in service.compile_execution_bundle(rebuilt)
    assert "Needs Replan: yes" in service.compile_execution_bundle(rebuilt)


def test_worker_result_reporting_updates_projection_and_artifact(tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run(
        "report-demo",
        project_profile="web",
        spec_content="spec",
        rules_content="rules",
        repo=str(repo),
    )
    worker = WorkerRuntime(
        worker_name="worker1",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        workspace_path=str(tmp_path / "workspace"),
        branch=f"branchclaw/{projection.run.id}/worker1",
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
        projection.run.id,
        "worker.started",
        {"worker": json.loads(worker.model_dump_json())},
    )

    runtime = service.report_worker_result(
        projection.run.id,
        "worker1",
        {
            "status": "success",
            "stack": "node",
            "runtime": "node",
            "package_manager": "pnpm",
            "install_command": "pnpm install",
            "start_command": "pnpm dev",
            "preview_url": "http://127.0.0.1:3000",
            "changed_surface_summary": "Updated the landing page hero and CTA.",
            "architecture_summary": "# Architecture Change Summary\n\n- Changed areas: `app`\n",
        },
    )

    assert runtime.result is not None
    assert runtime.result.project_profile.value == "web"
    assert runtime.result.preview_url == "http://127.0.0.1:3000"

    rebuilt = service.get_run(projection.run.id, rebuild=True)
    reported = rebuilt.workers["worker1"].result
    assert reported is not None
    assert reported.status.value == "success"
    assert reported.preview_url == "http://127.0.0.1:3000"
    assert reported.start_command == "pnpm dev"
    assert rebuilt.workers["worker1"].report_source == "operator"
    artifact = artifacts_dir(projection.run.id) / "worker-results" / "worker1.json"
    assert artifact.exists()
    assert json.loads(artifact.read_text(encoding="utf-8"))["preview_url"] == "http://127.0.0.1:3000"


def test_request_worker_shutdown_terminates_active_service_target(monkeypatch):
    service = BranchClawService()
    terminated: list[str] = []

    monkeypatch.setattr("branchclaw.service.tmux_target_alive", lambda target: target == "preview:app")
    monkeypatch.setattr("branchclaw.service.terminate_tmux_target", lambda target: terminated.append(target))

    worker = WorkerRuntime(
        worker_name="worker1",
        run_id="run-demo",
        stage_id="stage-1",
        workspace_path="/tmp/workspace",
        branch="branchclaw/run-demo/stage-1/worker1",
        base_ref="main",
        backend="subprocess",
        pid=0,
        child_pid=0,
        supervisor_pid=0,
        tmux_target="",
        task="serve preview",
        active_service_target="preview:app",
        heartbeat_at=now_iso(),
        last_heartbeat_at=now_iso(),
        started_at=now_iso(),
        status=WorkerStatus.running,
    )

    service._request_worker_shutdown(worker)

    assert terminated == ["preview:app"]


def test_event_export_omits_heartbeat_events_by_default(tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("export-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    worker = WorkerRuntime(
        worker_name="worker1",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        workspace_path=str(tmp_path / "workspace"),
        branch=f"branchclaw/{projection.run.id}/worker1",
        base_ref="main",
        backend="tmux",
        pid=123,
        child_pid=123,
        supervisor_pid=456,
        tmux_target="branchclaw-demo:worker1",
        task="build preview",
        heartbeat_at=now_iso(),
        last_heartbeat_at=now_iso(),
        started_at=now_iso(),
        status=WorkerStatus.running,
    )
    service.store.append(
        projection.run.id,
        "worker.started",
        {"worker": json.loads(worker.model_dump_json())},
    )
    service.store.append(
        projection.run.id,
        "worker.heartbeat",
        {"worker_name": "worker1", "last_heartbeat_at": now_iso()},
    )

    exported = service.export_events(projection.run.id)
    exported_with_heartbeats = service.export_events(projection.run.id, include_heartbeats=True)

    assert all(event["event_type"] != "worker.heartbeat" for event in exported["events"])
    assert any(
        event["event_type"] == "worker.heartbeat"
        for event in exported_with_heartbeats["events"]
    )


def test_constraint_blocks_archive_until_replan_is_approved(tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("replan-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    _, gate = service.propose_plan(projection.run.id, "Do work", summary="phase 1")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")
    service.add_constraint(projection.run.id, "No force pushes", author="human")

    with pytest.raises(BranchClawError):
        service.create_archive(projection.run.id, label="phase-1")

    _, replan_gate = service.propose_plan(projection.run.id, "Adjust plan", summary="phase 2")
    projection = service.approve_gate(projection.run.id, replan_gate.id, actor="reviewer")

    assert projection.run.needs_replan is False
    archive, archive_gate = service.create_archive(projection.run.id, label="phase-1")
    assert archive.id.startswith("archive-")
    assert archive_gate.id.startswith("gate-")


def test_plan_features_sync_and_auto_dispatch_respects_claim_conflicts(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()
    projection = service.create_run(
        "feature-demo",
        spec_content="spec",
        rules_content="rules",
        repo=str(repo),
        default_backend="subprocess",
        default_command=[sys.executable, "-c", "import time; time.sleep(1)"],
    )
    plan_content = """
## Feature: Hero Polish
Goal: Improve the homepage hero treatment.
Task: Update hero copy and layout.
Areas: ui, hero
Priority: 10

## Feature: API Health
Goal: Add a backend health endpoint.
Task: Implement a lightweight health check.
Areas: api
Priority: 20

## Feature: Pricing Rewrite
Goal: Rewrite the pricing section copy.
Task: Update pricing section messaging.
Areas: ui
Priority: 30
"""
    _, gate = service.propose_plan(projection.run.id, plan_content, summary="feature rollout", author="planner")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")

    spawned: list[tuple[str, str]] = []

    def _fake_spawn_worker(
        run_id: str,
        worker_name: str,
        *,
        command: list[str],
        backend: str = "subprocess",
        task: str = "",
        feature_id: str = "",
        **_: object,
    ) -> WorkerRuntime:
        spawned.append((worker_name, feature_id))
        return WorkerRuntime(
            worker_name=worker_name,
            run_id=run_id,
            stage_id=service.get_run(run_id).run.current_stage_id,
            workspace_path=str(tmp_path / worker_name),
            branch=f"branchclaw/{run_id}/{worker_name}",
            base_ref="main",
            backend=backend,
            pid=0,
            child_pid=0,
            supervisor_pid=0,
            tmux_target="",
            task=task,
            feature_id=feature_id,
            heartbeat_at=now_iso(),
            last_heartbeat_at=now_iso(),
            started_at=now_iso(),
            status=WorkerStatus.starting,
        )

    monkeypatch.setattr(service, "spawn_worker", _fake_spawn_worker)

    dispatched = service.dispatch_feature_backlog(projection.run.id)
    by_title = {feature.title: feature for feature in dispatched.features.values()}

    assert set(by_title) == {"Hero Polish", "API Health", "Pricing Rewrite"}
    assert by_title["Hero Polish"].status == FeatureStatus.assigned
    assert by_title["API Health"].status == FeatureStatus.assigned
    assert by_title["Pricing Rewrite"].status == FeatureStatus.queued
    assert len(spawned) == 2
    assert {feature_id for _, feature_id in spawned} == {
        by_title["Hero Polish"].id,
        by_title["API Health"].id,
    }


def test_batch_merge_and_promote_flow_uses_integration_ref(tmp_path):
    repo = _init_git_repo(tmp_path)
    feature_sha = _create_branch_commit(repo, "feature/hero", "app.py", "print('hero')\n")
    service = BranchClawService()
    projection = service.create_run(
        "batch-demo",
        spec_content="spec",
        rules_content="rules",
        repo=str(repo),
        integration_ref="branchclaw/test/integration",
    )
    _, gate = service.propose_plan(projection.run.id, "ship it", summary="phase 1")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")

    feature = FeatureRecord(
        id="feature-hero",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        title="Hero Polish",
        goal="Ship the hero feature.",
        task="Merge the hero branch.",
        claimed_areas=["ui"],
        priority=10,
        status=FeatureStatus.ready,
        validation_status=ValidationStatus.passed,
        snapshot_branch="feature/hero",
        snapshot_head_sha=feature_sha,
        result_summary="Hero branch is ready.",
    )
    batch = BatchRecord(
        id="batch-hero",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        feature_ids=[feature.id],
        status=BatchStatus.pending_approval,
        integration_ref="branchclaw/test/integration",
    )
    service.store.append(projection.run.id, "feature.created", {"feature": feature.model_dump(mode="json")})
    service.store.append(projection.run.id, "feature.ready", {"feature": feature.model_dump(mode="json")})
    service.store.append(projection.run.id, "batch.proposed", {"batch": batch.model_dump(mode="json")})

    merge_gate = service.request_merge(projection.run.id, batch_id=batch.id, actor="reviewer")
    service.approve_gate(projection.run.id, merge_gate.id, actor="reviewer")
    merged_projection = service.get_run(projection.run.id, rebuild=True)

    assert merged_projection.batches[batch.id].status == BatchStatus.pending_promote
    integration_contents = (
        subprocess.run(
            ["git", "show", f"{merged_projection.run.integration_ref}:app.py"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert "hero" in integration_contents

    promote_gate = service.request_promote(projection.run.id, batch_id=batch.id, actor="reviewer")
    service.approve_gate(projection.run.id, promote_gate.id, actor="reviewer")
    promoted_projection = service.get_run(projection.run.id, rebuild=True)

    assert promoted_projection.batches[batch.id].status == BatchStatus.completed
    assert promoted_projection.features[feature.id].status == FeatureStatus.merged
    main_contents = (
        subprocess.run(
            ["git", "show", "main:app.py"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert "hero" in main_contents


def test_batch_integration_failure_returns_features_to_ready(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path)
    feature_sha = _create_branch_commit(repo, "feature/hero", "app.py", "print('hero')\n")
    service = BranchClawService()
    projection = service.create_run(
        "batch-fail",
        spec_content="spec",
        rules_content="rules",
        repo=str(repo),
        integration_ref="branchclaw/test/integration",
    )
    _, gate = service.propose_plan(projection.run.id, "ship it", summary="phase 1")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")

    feature = FeatureRecord(
        id="feature-hero",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        title="Hero Polish",
        goal="Ship the hero feature.",
        task="Merge the hero branch.",
        claimed_areas=["ui"],
        priority=10,
        status=FeatureStatus.ready,
        validation_status=ValidationStatus.passed,
        snapshot_branch="feature/hero",
        snapshot_head_sha=feature_sha,
        result_summary="Hero branch is ready.",
    )
    batch = BatchRecord(
        id="batch-hero",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        feature_ids=[feature.id],
        status=BatchStatus.pending_approval,
        integration_ref="branchclaw/test/integration",
    )
    service.store.append(projection.run.id, "feature.created", {"feature": feature.model_dump(mode="json")})
    service.store.append(projection.run.id, "feature.ready", {"feature": feature.model_dump(mode="json")})
    service.store.append(projection.run.id, "batch.proposed", {"batch": batch.model_dump(mode="json")})
    monkeypatch.setattr(
        service,
        "_run_integration_validation",
        lambda projection: {"ok": False, "command": "npm run build", "output": "build failed"},
    )

    merge_gate = service.request_merge(projection.run.id, batch_id=batch.id, actor="reviewer")
    service.approve_gate(projection.run.id, merge_gate.id, actor="reviewer")
    failed_projection = service.get_run(projection.run.id, rebuild=True)

    assert failed_projection.batches[batch.id].status == BatchStatus.integration_failed
    assert failed_projection.features[feature.id].status == FeatureStatus.ready
    assert failed_projection.features[feature.id].integration_blocker == "build failed"


def test_archive_restore_recreates_workspace_without_overwriting_previous(tmp_path, branchclaw_daemon):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("restore-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    _, gate = service.propose_plan(projection.run.id, "Do work", summary="phase 1")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")
    worker = service.spawn_worker(
        projection.run.id,
        "worker1",
        command=["python3", "-c", "import time; time.sleep(60)"],
        backend="subprocess",
        task="touch app",
    )
    service.report_worker_result(
        projection.run.id,
        "worker1",
        {
            "status": "success",
            "stack": "python",
            "runtime": "python",
            "output_snippet": "app.py updated and validated",
            "changed_surface_summary": "Updated the backend behavior in app.py.",
            "architecture_summary": "# Architecture Change Summary\n\n- Changed areas: `app.py`\n",
        },
    )
    time.sleep(0.1)
    service.stop_worker(projection.run.id, "worker1")
    archive, archive_gate = service.create_archive(projection.run.id, label="phase-1")
    service.approve_gate(projection.run.id, archive_gate.id, actor="reviewer")
    rollback_gate = service.request_restore(projection.run.id, archive.id, actor="reviewer")
    projection = service.approve_gate(projection.run.id, rollback_gate.id, actor="reviewer")

    restored_worker = projection.workers["worker1"]
    summary = summarize_run(projection.run.id, service)
    assert projection.run.status.value == "rolled_back"
    assert worker.workspace_path != restored_worker.workspace_path
    assert Path(worker.workspace_path).exists()
    assert Path(restored_worker.workspace_path).exists()
    assert summary["worktreeTrack"]["summary"]["trackedWorkers"] == 1
    assert summary["worktreeTrack"]["summary"]["currentWorktrees"] == 1
    assert summary["worktreeTrack"]["summary"]["restoredWorktrees"] == 1
    assert summary["worktreeTrack"]["summary"]["archivedSnapshots"] == 1
    assert summary["worktreeTrack"]["summary"]["acceptedEntries"] == 2
    assert summary["worktreeTrack"]["resultStatusCounts"]["success"] == 2
    assert summary["worktreeTrack"]["tracks"][0]["entries"][0]["resultStatus"] == "success"
    assert summary["worktreeTrack"]["tracks"][0]["entries"][0]["outputSnippet"] == "app.py updated and validated"
    assert summary["worktreeTrack"]["tracks"][0]["entries"][-1]["kind"] == "restored"
    assert summary["worktreeTrack"]["tracks"][0]["entries"][-1]["resultStatus"] == "success"


def test_merge_conflict_marks_run_blocked(tmp_path, branchclaw_daemon):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("merge-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    _, gate = service.propose_plan(projection.run.id, "Change app.py", summary="merge phase")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")
    worker = service.spawn_worker(
        projection.run.id,
        "worker1",
        command=["python3", "-c", "import time; time.sleep(60)"],
        backend="subprocess",
        task="change app.py",
    )

    worker_file = Path(worker.workspace_path) / "app.py"
    worker_file.write_text("print('worker change')\n", encoding="utf-8")
    service.checkpoint_worker(projection.run.id, "worker1", message="worker change")
    service.stop_worker(projection.run.id, "worker1")

    (repo / "app.py").write_text("print('base change')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base change"], cwd=repo, check=True, capture_output=True)

    archive, archive_gate = service.create_archive(projection.run.id, label="phase-1")
    service.approve_gate(projection.run.id, archive_gate.id, actor="reviewer")
    merge_gate = service.request_merge(projection.run.id, archive_id=archive.id, actor="reviewer")
    projection = service.approve_gate(projection.run.id, merge_gate.id, actor="reviewer")

    assert projection.run.status.value == "merge_blocked"


def test_supervised_worker_emits_heartbeats_and_records_failure(tmp_path, monkeypatch, branchclaw_daemon):
    monkeypatch.setenv("BRANCHCLAW_HEARTBEAT_INTERVAL", "0.1")
    monkeypatch.setenv("BRANCHCLAW_SUPERVISOR_START_TIMEOUT", "5")
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("worker-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    _, gate = service.propose_plan(projection.run.id, "Run worker", summary="phase 1")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")
    worker = service.spawn_worker(
        projection.run.id,
        "worker1",
        command=[sys.executable, "-c", "import time; time.sleep(0.3); raise SystemExit(3)"],
        backend="subprocess",
        task="fail quickly",
    )

    assert worker.supervisor_pid > 0
    deadline = time.time() + 3.0
    runtime = None
    while time.time() < deadline:
        projection = service.reconcile_workers(projection.run.id)
        runtime = projection.workers["worker1"]
        if runtime.status.value == "failed":
            break
        time.sleep(0.1)

    assert runtime is not None
    assert runtime.last_heartbeat_at
    assert runtime.status.value == "failed"
    assert runtime.exit_code == 3
    assert runtime.failure_reason


def test_watchdog_blocks_failed_worker_without_progress(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("blocked-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    worker = WorkerRuntime(
        worker_name="worker1",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        workspace_path=str(repo),
        branch=f"branchclaw/{projection.run.id}/worker1",
        base_ref="main",
        backend="tmux",
        pid=123,
        child_pid=123,
        supervisor_pid=456,
        tmux_target="branchclaw-demo:worker1",
        task="install deps",
        heartbeat_at=now_iso(),
        last_heartbeat_at=now_iso(),
        started_at=now_iso(),
        status=WorkerStatus.running,
    )
    service.store.append(
        projection.run.id,
        "worker.started",
        {"worker": json.loads(worker.model_dump_json())},
    )
    service.store.append(
        projection.run.id,
        "worker.tool_failed",
        {
            "worker_name": "worker1",
            "tool_name": "project.install_dependencies",
            "error": "corepack missing",
            "diff_signature": "",
        },
    )

    shutdown_requests: list[str] = []
    monkeypatch.setenv("BRANCHCLAW_WORKER_BLOCK_AFTER", "30")
    monkeypatch.setenv("BRANCHCLAW_WORKER_AUTO_REMEDIATION_LIMIT", "0")
    monkeypatch.setattr("branchclaw.service.seconds_since", lambda _value: 120.0)
    monkeypatch.setattr(
        BranchClawService,
        "_request_worker_shutdown",
        lambda self, worker: shutdown_requests.append(worker.worker_name),
    )

    projection = service._apply_worker_watchdog_policies_local(projection.run.id)
    runtime = projection.workers["worker1"]

    assert runtime.status == WorkerStatus.blocked
    assert "no progress was detected" in runtime.blocked_reason
    assert runtime.failure_reason == runtime.blocked_reason
    assert shutdown_requests == ["worker1"]


def test_watchdog_blocks_failed_worker_after_retry_limit(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("retry-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    worker = WorkerRuntime(
        worker_name="worker1",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        workspace_path=str(repo),
        branch=f"branchclaw/{projection.run.id}/worker1",
        base_ref="main",
        backend="tmux",
        pid=123,
        child_pid=123,
        supervisor_pid=456,
        tmux_target="branchclaw-demo:worker1",
        task="install deps",
        heartbeat_at=now_iso(),
        last_heartbeat_at=now_iso(),
        started_at=now_iso(),
        status=WorkerStatus.running,
    )
    service.store.append(
        projection.run.id,
        "worker.started",
        {"worker": json.loads(worker.model_dump_json())},
    )
    for _ in range(3):
        service.store.append(
            projection.run.id,
            "worker.tool_failed",
            {
                "worker_name": "worker1",
                "tool_name": "project.install_dependencies",
                "error": "corepack missing",
                "diff_signature": "",
            },
        )

    shutdown_requests: list[str] = []
    monkeypatch.setenv("BRANCHCLAW_WORKER_TOOL_RETRY_LIMIT", "2")
    monkeypatch.setenv("BRANCHCLAW_WORKER_AUTO_REMEDIATION_LIMIT", "0")
    monkeypatch.setattr("branchclaw.service.seconds_since", lambda _value: 0.0)
    monkeypatch.setattr(
        BranchClawService,
        "_request_worker_shutdown",
        lambda self, worker: shutdown_requests.append(worker.worker_name),
    )

    projection = service._apply_worker_watchdog_policies_local(projection.run.id)
    runtime = projection.workers["worker1"]

    assert runtime.status == WorkerStatus.blocked
    assert "failed 3 times" in runtime.blocked_reason
    assert runtime.tool_retry_count == 3
    assert shutdown_requests == ["worker1"]


def test_watchdog_attempts_safe_remediation_and_auto_restart(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("remediation-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    worker = WorkerRuntime(
        worker_name="worker1",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        workspace_path=str(repo),
        branch=f"branchclaw/{projection.run.id}/worker1",
        base_ref="main",
        backend="tmux",
        pid=123,
        child_pid=123,
        supervisor_pid=456,
        tmux_target="branchclaw-demo:worker1",
        task="install deps",
        heartbeat_at=now_iso(),
        last_heartbeat_at=now_iso(),
        started_at=now_iso(),
        status=WorkerStatus.running,
    )
    service.store.append(
        projection.run.id,
        "worker.started",
        {"worker": json.loads(worker.model_dump_json())},
    )
    service.store.append(
        projection.run.id,
        "worker.tool_failed",
        {
            "worker_name": "worker1",
            "tool_name": "project.install_dependencies",
            "arguments": {"repo_root": "."},
            "error": "[Errno 2] No such file or directory: 'corepack'",
            "diff_signature": "",
        },
    )

    monkeypatch.setenv("BRANCHCLAW_WORKER_AUTO_REMEDIATION_LIMIT", "2")
    monkeypatch.setenv("BRANCHCLAW_WORKER_AUTO_RESTART_LIMIT", "1")
    monkeypatch.setattr(
        "branchclaw.service.install_dependencies",
        lambda repo_root, project_info: {
            "ok": True,
            "command": ["npm", "install"],
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        BranchClawService,
        "_auto_restart_worker",
        lambda self, current: WorkerRuntime(
            **{
                **json.loads(current.model_dump_json()),
                "status": WorkerStatus.running,
                "restart_attempt_count": current.restart_attempt_count + 1,
            }
        ),
    )

    projection = service._apply_worker_watchdog_policies_local(projection.run.id)
    runtime = projection.workers["worker1"]
    event_types = [event.event_type for event in EventStore().list_events(projection.run.id)]

    assert "worker.remediation_attempted" in event_types
    assert "worker.remediation_succeeded" in event_types
    assert runtime.last_remediation_action == "fallback_install_npm"
    assert runtime.last_remediation_status == "succeeded"
    assert runtime.restart_attempt_count == 1
    assert projection.stats.open_intervention_count == 0


def test_watchdog_opens_intervention_when_remediation_budget_is_exhausted(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("intervention-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    worker = WorkerRuntime(
        worker_name="worker1",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        workspace_path=str(repo),
        branch=f"branchclaw/{projection.run.id}/worker1",
        base_ref="main",
        backend="tmux",
        pid=123,
        child_pid=123,
        supervisor_pid=456,
        tmux_target="branchclaw-demo:worker1",
        task="install deps",
        heartbeat_at=now_iso(),
        last_heartbeat_at=now_iso(),
        started_at=now_iso(),
        status=WorkerStatus.running,
        remediation_attempt_count=2,
        restart_attempt_count=1,
    )
    service.store.append(
        projection.run.id,
        "worker.started",
        {"worker": json.loads(worker.model_dump_json())},
    )
    service.store.append(
        projection.run.id,
        "worker.tool_failed",
        {
            "worker_name": "worker1",
            "tool_name": "project.install_dependencies",
            "arguments": {"repo_root": "."},
            "error": "[Errno 2] No such file or directory: 'corepack'",
            "diff_signature": "",
        },
    )

    shutdown_requests: list[str] = []
    monkeypatch.setenv("BRANCHCLAW_WORKER_AUTO_REMEDIATION_LIMIT", "2")
    monkeypatch.setenv("BRANCHCLAW_WORKER_AUTO_RESTART_LIMIT", "1")
    monkeypatch.setenv("BRANCHCLAW_WORKER_BLOCK_AFTER", "1")
    monkeypatch.setattr("branchclaw.service.seconds_since", lambda _value: 120.0)
    monkeypatch.setattr(
        BranchClawService,
        "_request_worker_shutdown",
        lambda self, current: shutdown_requests.append(current.worker_name),
    )

    projection = service._apply_worker_watchdog_policies_local(projection.run.id)
    runtime = projection.workers["worker1"]
    interventions = [item for item in projection.interventions.values() if item.status.value == "open"]

    assert runtime.status == WorkerStatus.blocked
    assert shutdown_requests == ["worker1"]
    assert len(interventions) == 1
    assert interventions[0].worker_name == "worker1"
    assert interventions[0].recommended_action in {"open_review", "restart_worker"}


def test_blocked_worker_status_survives_heartbeat_and_stop_events(tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("blocked-state-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    worker = WorkerRuntime(
        worker_name="worker1",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        workspace_path=str(repo),
        branch=f"branchclaw/{projection.run.id}/worker1",
        base_ref="main",
        backend="tmux",
        pid=123,
        child_pid=123,
        supervisor_pid=456,
        tmux_target="branchclaw-demo:worker1",
        task="install deps",
        heartbeat_at=now_iso(),
        last_heartbeat_at=now_iso(),
        started_at=now_iso(),
        status=WorkerStatus.running,
    )
    service.store.append(
        projection.run.id,
        "worker.started",
        {"worker": json.loads(worker.model_dump_json())},
    )
    service.store.append(
        projection.run.id,
        "worker.blocked",
        {
            "worker_name": "worker1",
            "blocked_reason": "manual intervention required",
            "tool_name": "project.install_dependencies",
            "tool_retry_count": 3,
            "last_tool_error": "corepack missing",
        },
    )
    service.store.append(
        projection.run.id,
        "worker.heartbeat",
        {
            "worker_name": "worker1",
            "last_heartbeat_at": now_iso(),
        },
    )
    service.store.append(
        projection.run.id,
        "worker.stopped",
        {
            "worker_name": "worker1",
            "finished_at": now_iso(),
            "explicit_stop": True,
        },
    )

    runtime = service.get_run(projection.run.id, rebuild=True).workers["worker1"]

    assert runtime.status == WorkerStatus.blocked
    assert runtime.blocked_reason == "manual intervention required"
    assert runtime.failure_reason == "manual intervention required"
    assert runtime.last_tool_status == "failed"


def test_reconcile_marks_unreachable_worker_failed(tmp_path, monkeypatch, branchclaw_daemon):
    monkeypatch.setenv("BRANCHCLAW_HEARTBEAT_INTERVAL", "0.1")
    monkeypatch.setenv("BRANCHCLAW_STALE_AFTER", "0.2")
    monkeypatch.setenv("BRANCHCLAW_SUPERVISOR_START_TIMEOUT", "5")
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("reconcile-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    _, gate = service.propose_plan(projection.run.id, "Run worker", summary="phase 1")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")
    worker = service.spawn_worker(
        projection.run.id,
        "worker1",
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
        backend="subprocess",
        task="sleep",
    )

    os.kill(worker.supervisor_pid, signal.SIGKILL)
    os.kill(worker.child_pid or worker.pid, signal.SIGKILL)
    deadline = time.time() + 3.0
    runtime = None
    while time.time() < deadline:
        projection = service.reconcile_workers(projection.run.id)
        runtime = projection.workers["worker1"]
        if runtime.status.value == "failed":
            break
        time.sleep(0.1)

    assert runtime is not None
    assert runtime.status.value == "failed"
    assert runtime.failure_reason


def test_migrate_from_clawteam_imports_legacy_artifacts(tmp_path, monkeypatch):
    from clawteam.team.manager import TeamManager
    from clawteam.team.tasks import TaskStore

    legacy_root = tmp_path / ".legacy"
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(legacy_root))
    TeamManager.create_team("legacy", leader_name="leader", leader_id="leader001")
    TaskStore("legacy").create("legacy task", owner="leader")
    team_events_dir = legacy_root / "teams" / "legacy" / "events"
    team_events_dir.mkdir(parents=True, exist_ok=True)
    (team_events_dir / "evt-000001.json").write_text(
        json.dumps({"type": "message", "from": "leader", "content": "hello"}),
        encoding="utf-8",
    )

    repo = _init_git_repo(tmp_path)
    service = BranchClawService()
    projection = service.migrate_from_clawteam("legacy", repo=str(repo))

    art_dir = artifacts_dir(projection.run.id)
    assert (art_dir / "legacy-team-config.json").exists()
    assert (art_dir / "legacy-tasks.json").exists()
    assert (art_dir / "legacy-events.json").exists()
