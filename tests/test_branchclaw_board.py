from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from branchclaw.board import build_server
from branchclaw.models import (
    BatchRecord,
    BatchStatus,
    FeatureRecord,
    FeatureStatus,
    ValidationStatus,
)
from branchclaw.service import BranchClawService
from branchclaw.workspace import GitWorkspaceRuntimeAdapter


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


def _start_board(service: BranchClawService):
    server = build_server(host="127.0.0.1", port=0, poll_interval=0.05, service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _json_get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_branchclaw_board_http_surfaces_runs_projection_and_sse(tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("board-demo", spec_content="spec", rules_content="rules", repo=str(repo))
    _, gate = service.propose_plan(projection.run.id, "ship it", summary="phase 1")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")
    service.add_constraint(projection.run.id, "No force pushes", author="human")

    server, thread, base_url = _start_board(service)
    try:
        runs = _json_get(f"{base_url}/api/runs")
        assert runs
        assert runs[0]["id"] == projection.run.id
        assert runs[0]["needsReplan"] is True
        assert runs[0]["dataDirKey"]
        assert runs[0]["ownerDataDir"]

        daemon_status = _json_get(f"{base_url}/api/daemon/status")
        assert daemon_status["dashboard_running"] is True
        assert daemon_status["dashboard_url"] == base_url

        data_dirs = _json_get(f"{base_url}/api/data-dirs")
        assert len(data_dirs) == 1
        assert data_dirs[0]["dataDirKey"] == runs[0]["dataDirKey"]

        processes = _json_get(f"{base_url}/api/processes")
        assert processes == []

        payload = _json_get(f"{base_url}/api/run/{projection.run.id}")
        assert payload["run"]["id"] == projection.run.id
        assert payload["run"]["needsReplan"] is True
        assert payload["run"]["projectProfile"] == "backend"
        assert payload["run"]["openInterventionCount"] == 0
        assert payload["interventions"] == []
        assert payload["worktreeTrack"]["summary"]["trackedWorkers"] == 0
        assert payload["constraints"][0]["content"] == "No force pushes"

        scoped_payload = _json_get(
            f"{base_url}/api/data-dirs/{runs[0]['dataDirKey']}/runs/{projection.run.id}"
        )
        assert scoped_payload["run"]["id"] == projection.run.id

        recent_events = _json_get(
            f"{base_url}/api/data-dirs/{runs[0]['dataDirKey']}/runs/{projection.run.id}/recent-events?limit=10"
        )
        assert recent_events
        assert all(item["level"] in {"info", "warning", "error"} for item in recent_events)

        with urllib.request.urlopen(f"{base_url}/", timeout=5) as response:
            picker_html = response.read().decode("utf-8")
        assert "<h1>BranchClaw</h1>" in picker_html
        assert "Choose a Workdir and Run" in picker_html
        assert "Create Run" in picker_html
        assert "Workdir must point directly to a" in picker_html
        assert 'data-page="picker"' in picker_html
        assert 'id="page-picker"' in picker_html
        assert 'id="page-workspace"' not in picker_html

        with urllib.request.urlopen(f"{base_url}/workspace.html", timeout=5) as response:
            workspace_html = response.read().decode("utf-8")
        assert "Worktree Master" in workspace_html
        assert "Feature Queue" in workspace_html
        assert "Batch Review" in workspace_html
        assert "Needs Attention" in workspace_html
        assert "Add Worktree" in workspace_html
        assert 'data-page="workspace"' in workspace_html
        assert 'id="page-workspace"' in workspace_html
        assert 'id="page-review"' not in workspace_html

        with urllib.request.urlopen(f"{base_url}/review.html", timeout=5) as response:
            review_html = response.read().decode("utf-8")
        assert "Review This Worktree" in review_html
        assert "Pending Decisions" in review_html
        assert "Activity" in review_html
        assert "Archives" in review_html
        assert "Events" in review_html
        assert "Run Details" in review_html
        assert "Info" in review_html
        assert "Warning" in review_html
        assert "Error" in review_html
        assert 'data-page="review"' in review_html
        assert 'id="page-review"' in review_html
        assert 'id="page-control-plane"' not in review_html

        with urllib.request.urlopen(f"{base_url}/control-plane.html", timeout=5) as response:
            control_plane_html = response.read().decode("utf-8")
        assert "Control Plane" in control_plane_html
        assert "Intervention Queue" in control_plane_html
        assert "Runs Needing Attention" in control_plane_html
        assert "Tracked Workdirs" in control_plane_html
        assert "Managed Processes" in control_plane_html
        assert 'data-page="control-plane"' in control_plane_html
        assert 'id="page-control-plane"' in control_plane_html

        with urllib.request.urlopen(f"{base_url}/static/board.css", timeout=5) as response:
            css = response.read().decode("utf-8")
        assert ".page-tab.active" in css
        assert ".review-tab-layout" in css

        with urllib.request.urlopen(f"{base_url}/static/board.js", timeout=5) as response:
            js = response.read().decode("utf-8")
        assert "function buildPageUrl" in js
        assert "function renderReviewPanel" in js

        conn = http.client.HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
        conn.request("GET", f"/api/events/{projection.run.id}")
        response = conn.getresponse()
        assert response.status == 200
        line = response.fp.readline().decode("utf-8")
        assert line.startswith("data: ")
        sse_payload = json.loads(line[len("data: "):].strip())
        assert sse_payload["run"]["id"] == projection.run.id
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_branchclaw_board_http_actions_cover_gate_archive_restore_merge_and_worker_stop(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("BRANCHCLAW_HEARTBEAT_INTERVAL", "0.1")
    monkeypatch.setenv("BRANCHCLAW_SUPERVISOR_START_TIMEOUT", "5")

    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("board-actions", spec_content="spec", rules_content="rules", repo=str(repo))
    _, gate = service.propose_plan(projection.run.id, "ship it", summary="phase 1")
    workspace = GitWorkspaceRuntimeAdapter(str(repo)).create_workspace(
        projection.run.id,
        projection.run.current_stage_id,
        "worker-archive",
    )
    worker = {
        "worker_name": "worker-archive",
        "run_id": projection.run.id,
        "stage_id": projection.run.current_stage_id,
        "workspace_path": workspace.workspace_path,
        "branch": workspace.branch,
        "base_ref": workspace.base_ref,
        "head_sha": workspace.head_sha,
        "backend": "tmux",
        "pid": 0,
        "child_pid": 0,
        "supervisor_pid": 0,
        "tmux_target": "",
        "task": "archive candidate",
        "heartbeat_at": "2026-03-22T00:00:00+00:00",
        "last_heartbeat_at": "2026-03-22T00:00:00+00:00",
        "started_at": "2026-03-22T00:00:00+00:00",
        "finished_at": "2026-03-22T00:10:00+00:00",
        "status": "stopped",
    }
    service.store.append(projection.run.id, "worker.started", {"worker": worker})
    server, thread, base_url = _start_board(service)
    try:
        runs = _json_get(f"{base_url}/api/runs")
        data_key = runs[0]["dataDirKey"]

        approved = urllib.request.Request(
            f"{base_url}/api/data-dirs/{data_key}/runs/{projection.run.id}/gates/{gate.id}/approve",
            data=json.dumps({"actor": "reviewer"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(approved, timeout=5) as response:
            approved_body = json.loads(response.read().decode("utf-8"))
        assert approved_body["approved"] is True

        archive_request = urllib.request.Request(
            f"{base_url}/api/data-dirs/{data_key}/runs/{projection.run.id}/archives",
            data=json.dumps({"label": "checkpoint", "summary": "test", "actor": "reviewer"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(archive_request, timeout=5) as response:
            archive_body = json.loads(response.read().decode("utf-8"))
        assert archive_body["archiveId"].startswith("archive-")
        assert archive_body["gateId"].startswith("gate-")

        archive_snapshot = _json_get(
            f"{base_url}/api/data-dirs/{data_key}/runs/{projection.run.id}"
        )
        archive_gate = next(item for item in archive_snapshot["approvals"] if item["id"] == archive_body["gateId"])
        assert archive_gate["relatedEntryIds"]
        assert archive_gate["relatedArchiveId"] == archive_body["archiveId"]

        approve_archive = urllib.request.Request(
            f"{base_url}/api/data-dirs/{data_key}/runs/{projection.run.id}/gates/{archive_body['gateId']}/approve",
            data=json.dumps({"actor": "reviewer"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(approve_archive, timeout=5) as response:
            archive_gate_body = json.loads(response.read().decode("utf-8"))
        assert archive_gate_body["approved"] is True

        restore_request = urllib.request.Request(
            f"{base_url}/api/data-dirs/{data_key}/runs/{projection.run.id}/archives/{archive_body['archiveId']}/restore",
            data=json.dumps({"actor": "reviewer"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(restore_request, timeout=5) as response:
            restore_body = json.loads(response.read().decode("utf-8"))
        assert restore_body["archiveId"] == archive_body["archiveId"]
        assert restore_body["gateId"].startswith("gate-")

        merge_request = urllib.request.Request(
            f"{base_url}/api/data-dirs/{data_key}/runs/{projection.run.id}/merge-request",
            data=json.dumps({"actor": "reviewer", "archiveId": archive_body["archiveId"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(merge_request, timeout=5) as response:
            merge_body = json.loads(response.read().decode("utf-8"))
        assert merge_body["archiveId"] == archive_body["archiveId"]
        assert merge_body["gateId"].startswith("gate-")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_branchclaw_board_payload_surfaces_open_interventions(tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("board-intervention", spec_content="spec", rules_content="rules", repo=str(repo))
    worker = {
        "worker_name": "worker-watch",
        "run_id": projection.run.id,
        "stage_id": projection.run.current_stage_id,
        "workspace_path": str(repo),
        "branch": f"branchclaw/{projection.run.id}/worker-watch",
        "base_ref": "main",
        "head_sha": "",
        "backend": "tmux",
        "pid": 0,
        "child_pid": 0,
        "supervisor_pid": 0,
        "tmux_target": "",
        "task": "install deps",
        "heartbeat_at": "2026-03-24T00:00:00+00:00",
        "last_heartbeat_at": "2026-03-24T00:00:00+00:00",
        "started_at": "2026-03-24T00:00:00+00:00",
        "status": "blocked",
        "blocked_reason": "manual intervention required",
        "failure_reason": "manual intervention required",
    }
    service.store.append(projection.run.id, "worker.started", {"worker": worker})
    service.store.append(
        projection.run.id,
        "worker.intervention_opened",
        {
            "intervention": {
                "id": "intr-test",
                "run_id": projection.run.id,
                "worker_name": "worker-watch",
                "status": "open",
                "reason": "tool failed and requires operator review",
                "recommended_action": "restart_worker",
                "last_tool_name": "project.install_dependencies",
                "last_tool_error": "corepack missing",
                "remediation_attempts": 2,
                "restart_attempts": 1,
                "related_entry_id": "",
                "created_at": "2026-03-24T00:01:00+00:00",
                "resolved_at": "",
                "resolution_reason": "",
            }
        },
    )

    server, thread, base_url = _start_board(service)
    try:
        runs = _json_get(f"{base_url}/api/runs")
        assert runs[0]["openInterventionCount"] == 1

        payload = _json_get(f"{base_url}/api/run/{projection.run.id}")
        assert payload["run"]["openInterventionCount"] == 1
        assert len(payload["interventions"]) == 1
        assert payload["interventions"][0]["recommended_action"] == "restart_worker"
        assert payload["interventions"][0]["relatedEntryId"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_branchclaw_board_projection_tracks_running_and_stopped_workers(tmp_path, monkeypatch, branchclaw_daemon):
    monkeypatch.setenv("BRANCHCLAW_HEARTBEAT_INTERVAL", "0.1")
    monkeypatch.setenv("BRANCHCLAW_SUPERVISOR_START_TIMEOUT", "5")

    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run("board-worker", spec_content="spec", rules_content="rules", repo=str(repo))
    _, gate = service.propose_plan(projection.run.id, "run worker", summary="phase 1")
    service.approve_gate(projection.run.id, gate.id, actor="reviewer")

    server, thread, base_url = _start_board(service)
    try:
        service.spawn_worker(
            projection.run.id,
            "worker1",
            command=[sys.executable, "-c", "import time; time.sleep(60)"],
            backend="subprocess",
            task="sleep",
        )
        running = _json_get(f"{base_url}/api/run/{projection.run.id}")
        assert running["workers"][0]["status"] == "running"
        assert running["workers"][0]["hasChild"] is True

        service.stop_worker(projection.run.id, "worker1")
        deadline = time.time() + 3.0
        stopped = None
        while time.time() < deadline:
            stopped = _json_get(f"{base_url}/api/run/{projection.run.id}")
            if stopped["workers"][0]["status"] == "stopped":
                break
            time.sleep(0.1)

        assert stopped is not None
        assert stopped["workers"][0]["status"] == "stopped"
        assert stopped["workers"][0]["hasChild"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_branchclaw_board_exposes_reported_worker_results(tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run(
        "board-result",
        project_profile="fullstack",
        spec_content="spec",
        rules_content="rules",
        repo=str(repo),
    )
    worker = {
        "worker_name": "worker-api",
        "run_id": projection.run.id,
        "stage_id": projection.run.current_stage_id,
        "workspace_path": str(tmp_path / "workspace"),
        "branch": f"branchclaw/{projection.run.id}/worker-api",
        "base_ref": "main",
        "backend": "tmux",
        "pid": 0,
        "child_pid": 0,
        "supervisor_pid": 0,
        "tmux_target": "",
        "task": "ship preview",
        "heartbeat_at": "2026-03-22T00:00:00+00:00",
        "last_heartbeat_at": "2026-03-22T00:00:00+00:00",
        "started_at": "2026-03-22T00:00:00+00:00",
        "status": "stopped",
    }
    service.store.append(projection.run.id, "worker.started", {"worker": worker})
    service.report_worker_result(
        projection.run.id,
        "worker-api",
        {
            "status": "success",
            "stack": "node",
            "runtime": "node",
            "preview_url": "http://127.0.0.1:3000",
            "backend_url": "http://127.0.0.1:4000",
            "changed_surface_summary": "Updated the dashboard and API route wiring.",
            "architecture_summary": "# Architecture Change Summary\n\n- Changed areas: `app`, `api`\n",
        },
        source="agent",
    )
    service.store.append(
        projection.run.id,
        "worker.tool_completed",
        {
            "worker_name": "worker-api",
            "tool_name": "service.discover_url",
            "result": {"url": "http://127.0.0.1:3000"},
        },
    )

    server, thread, base_url = _start_board(service)
    try:
        payload = _json_get(f"{base_url}/api/run/{projection.run.id}")
        assert payload["run"]["projectProfile"] == "fullstack"
        assert payload["worktreeTrack"]["summary"]["currentWorktrees"] == 1
        assert payload["worktreeTrack"]["summary"]["acceptedEntries"] == 1
        assert payload["worktreeTrack"]["resultStatusCounts"]["success"] == 1
        assert payload["worktreeTrack"]["tracks"][0]["entries"][0]["entryId"].startswith("entry-")
        assert payload["worktreeTrack"]["tracks"][0]["entries"][0]["resultStatus"] == "success"
        assert payload["worktreeTrack"]["tracks"][0]["entries"][0]["previewUrl"] == "http://127.0.0.1:3000"
        assert payload["workers"][0]["resultStatus"] == "success"
        assert payload["workers"][0]["previewUrl"] == "http://127.0.0.1:3000"
        assert payload["workers"][0]["backendUrl"] == "http://127.0.0.1:4000"
        assert payload["workers"][0]["reportSource"] == "agent"
        assert payload["workers"][0]["discoveredUrl"] == "http://127.0.0.1:3000"
        assert "dashboard and API route wiring" in payload["workers"][0]["changedSurfaceSummary"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_branchclaw_board_surfaces_feature_queue_and_batch_review(tmp_path):
    repo = _init_git_repo(tmp_path)
    service = BranchClawService()

    projection = service.create_run(
        "board-features",
        project_profile="web",
        spec_content="spec",
        rules_content="rules",
        repo=str(repo),
        direction="Ship a backlog of small homepage improvements.",
        integration_ref="branchclaw/board-features/integration",
        max_active_features=3,
    )
    feature_assigned = FeatureRecord(
        id="feature-ui",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        title="Hero Polish",
        goal="Improve the homepage hero.",
        task="Tune hero layout and copy.",
        status=FeatureStatus.assigned,
        claimed_areas=["ui", "hero"],
        priority=10,
        worker_name="feature-ui-worker",
        validation_status=ValidationStatus.pending,
    )
    feature_ready = FeatureRecord(
        id="feature-api",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        title="API Health",
        goal="Add an API health check.",
        task="Create a health endpoint.",
        status=FeatureStatus.ready,
        claimed_areas=["api"],
        priority=20,
        validation_status=ValidationStatus.passed,
        result_summary="Health endpoint is ready for review.",
    )
    batch = BatchRecord(
        id="batch-review",
        run_id=projection.run.id,
        stage_id=projection.run.current_stage_id,
        feature_ids=[feature_ready.id],
        status=BatchStatus.pending_promote,
        integration_ref="branchclaw/board-features/integration",
        validation_status=ValidationStatus.passed,
        validation_output="integration build passed",
    )
    service.store.append(projection.run.id, "feature.created", {"feature": feature_assigned.model_dump(mode="json")})
    service.store.append(projection.run.id, "feature.created", {"feature": feature_ready.model_dump(mode="json")})
    service.store.append(projection.run.id, "feature.ready", {"feature": feature_ready.model_dump(mode="json")})
    service.store.append(projection.run.id, "batch.proposed", {"batch": batch.model_dump(mode="json")})
    service.store.append(
        projection.run.id,
        "batch.integration_validated",
        {
            "batch_id": batch.id,
            "validation_command": "npm run build",
            "validation_output": "integration build passed",
        },
    )

    server, thread, base_url = _start_board(service)
    try:
        payload = _json_get(f"{base_url}/api/run/{projection.run.id}")
        assert payload["run"]["direction"] == "Ship a backlog of small homepage improvements."
        assert payload["run"]["integrationRef"] == "branchclaw/board-features/integration"
        assert payload["run"]["maxActiveFeatures"] == 3
        assert payload["run"]["readyFeatureCount"] == 0
        assert payload["run"]["openBatchCount"] == 1
        assert payload["run"]["activeClaims"]["claimedAreas"] == ["hero", "ui"]
        assert len(payload["features"]) == 2
        assert {item["title"] for item in payload["features"]} == {"Hero Polish", "API Health"}
        assert next(item for item in payload["features"] if item["id"] == "feature-api")["status"] == "batched"
        assert len(payload["batches"]) == 1
        assert payload["batches"][0]["id"] == "batch-review"
        assert payload["batches"][0]["status"] == "pending_promote"
        assert payload["batches"][0]["featureSummaries"][0]["title"] == "API Health"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
