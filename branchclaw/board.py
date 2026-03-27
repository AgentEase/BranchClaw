"""Board helpers for BranchClaw projections."""

from __future__ import annotations

import json
import shlex
import time
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from branchclaw.config import get_data_dir
from branchclaw.models import InterventionStatus, seconds_since, worktree_entry_id
from branchclaw.service import BranchClawService
from branchclaw.storage import EventStore

_LIVE_WORKER_STATUSES = {"starting", "running", "stale"}
_UNHEALTHY_WORKER_STATUSES = {"stale", "blocked", "failed"}


def _event_level(event_type: str, payload: dict | None = None) -> str:
    payload = payload or {}
    explicit = str(payload.get("level") or payload.get("log_level") or "").lower().strip()
    if explicit in {"debug", "info", "warning", "error"}:
        return "info" if explicit == "debug" else explicit

    status = str(payload.get("status") or payload.get("result", {}).get("status") or "").lower()
    event_type = event_type.lower()

    if any(token in event_type for token in ("failed", "rejected", "blocked")):
        return "error"
    if "intervention_opened" in event_type:
        return "error"
    if any(token in status for token in ("failed", "blocked", "error")):
        return "error"

    warning_tokens = (
        "requested",
        "stale",
        "replan",
        "remediation_attempted",
        "remediation_failed",
        "superseded",
        "merge.blocked",
    )
    if any(token in event_type for token in warning_tokens):
        return "warning"
    if status in {"pending", "pending_approval", "stale"}:
        return "warning"
    return "info"


def data_dir_key(data_dir: str) -> str:
    return sha1(str(Path(data_dir).resolve()).encode("utf-8")).hexdigest()[:12]


def _increment(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _relative_workspace_path(run_id: str, workspace_path: str) -> str:
    marker = f"/workspaces/{run_id}/"
    if marker in workspace_path:
        return workspace_path.split(marker, 1)[1]
    return workspace_path


def _result_view(result: dict | None) -> dict:
    payload = result or {}
    return {
        "result": payload,
        "resultStatus": payload.get("status", ""),
        "previewUrl": payload.get("preview_url", ""),
        "backendUrl": payload.get("backend_url", ""),
        "outputSnippet": payload.get("output_snippet", ""),
        "changedSurfaceSummary": payload.get("changed_surface_summary", ""),
        "architectureSummary": payload.get("architecture_summary", ""),
        "warnings": payload.get("warnings", []),
        "blockers": payload.get("blockers", []),
    }


def _worker_view(worker) -> dict:
    data = json.loads(worker.model_dump_json())
    heartbeat_age = seconds_since(worker.last_heartbeat_at or worker.heartbeat_at)
    data["heartbeatAgeSeconds"] = round(heartbeat_age or 0.0, 3)
    data["hasSupervisor"] = worker.supervisor_pid > 0
    data["hasChild"] = (worker.child_pid or worker.pid) > 0
    data["mcpEnabled"] = worker.mcp_enabled
    data["mcpServerUrl"] = worker.mcp_server_url
    data["mcpTokenId"] = worker.mcp_token_id
    data["lastToolName"] = worker.last_tool_name
    data["lastToolStatus"] = worker.last_tool_status
    data["lastToolAt"] = worker.last_tool_at
    data["lastToolError"] = worker.last_tool_error
    data["activeServiceTarget"] = worker.active_service_target
    data["activeServiceLogPath"] = worker.active_service_log_path
    data["discoveredUrl"] = worker.discovered_url
    data["reportSource"] = worker.report_source
    data["remediationAttemptCount"] = worker.remediation_attempt_count
    data["restartAttemptCount"] = worker.restart_attempt_count
    data["lastRemediationAction"] = worker.last_remediation_action
    data["lastRemediationStatus"] = worker.last_remediation_status
    data["lastRemediationAt"] = worker.last_remediation_at
    data["interventionId"] = worker.intervention_id
    data["managedByDaemon"] = worker.managed_by_daemon
    data["daemonPid"] = worker.daemon_pid
    data["managedProcessKind"] = "supervisor" if worker.managed_by_daemon else ""
    data["managedStatus"] = worker.status.value
    data["ownerDataDir"] = str(get_data_dir().resolve())
    data.update(_result_view(data.get("result")))
    return data


def _approval_view(gate, worktree_track: dict) -> dict:
    payload = json.loads(gate.model_dump_json())
    related_entry_ids: list[str] = []
    related_worker_names: set[str] = set()
    related_archive_id = ""

    for track in worktree_track.get("tracks", []):
        worker_name = track.get("workerName", "")
        for entry in track.get("entries", []):
            matches = False
            if gate.gate_type.value in {"archive", "merge", "rollback"}:
                if entry.get("archiveId") and entry.get("archiveId") == gate.target_id:
                    matches = True
                    related_archive_id = gate.target_id
            elif gate.gate_type.value == "plan":
                if gate.stage_id and entry.get("stageId") == gate.stage_id and entry.get("kind") != "archived":
                    matches = True
            if matches:
                related_entry_ids.append(entry.get("entryId", ""))
                if worker_name:
                    related_worker_names.add(worker_name)

    payload["relatedEntryIds"] = [item for item in related_entry_ids if item]
    payload["relatedWorkerNames"] = sorted(related_worker_names)
    payload["relatedArchiveId"] = related_archive_id
    return payload


def _intervention_views(projection, worktree_track: dict) -> list[dict]:
    entries_by_worker: dict[str, list[dict]] = {
        track.get("workerName", ""): track.get("entries", [])
        for track in worktree_track.get("tracks", [])
    }
    rows: list[dict] = []
    for intervention in sorted(
        projection.interventions.values(),
        key=lambda item: item.created_at,
        reverse=True,
    ):
        payload = json.loads(intervention.model_dump_json())
        if not payload.get("related_entry_id"):
            related_entries = entries_by_worker.get(intervention.worker_name, [])
            if related_entries:
                payload["relatedEntryId"] = related_entries[-1].get("entryId", "")
            else:
                payload["relatedEntryId"] = ""
        else:
            payload["relatedEntryId"] = payload["related_entry_id"]
        payload["relatedWorkerName"] = intervention.worker_name
        rows.append(payload)
    return rows


def _feature_view(feature) -> dict:
    payload = json.loads(feature.model_dump_json())
    payload["hasWorker"] = bool(feature.worker_name)
    payload["hasSnapshot"] = bool(feature.snapshot_branch or feature.snapshot_head_sha)
    payload["hasResult"] = feature.result is not None
    return payload


def _batch_view(batch, projection) -> dict:
    payload = json.loads(batch.model_dump_json())
    payload["featureSummaries"] = [
        {
            "id": feature.id,
            "title": feature.title,
            "status": feature.status.value,
            "workerName": feature.worker_name,
            "resultSummary": feature.result_summary,
        }
        for feature in (
            projection.features.get(feature_id)
            for feature_id in batch.feature_ids
        )
        if feature is not None
    ]
    return payload


def _active_claim_summary(projection) -> dict:
    active_features = [
        feature
        for feature in projection.features.values()
        if feature.status.value in {"assigned", "in_progress"}
    ]
    areas = sorted({area for feature in active_features for area in feature.claimed_areas})
    files = sorted({path for feature in active_features for path in feature.claimed_files})
    return {
        "activeFeatureCount": len(active_features),
        "claimedAreas": areas,
        "claimedFiles": files,
    }


def _worktree_track(projection) -> dict:
    current_status_counts: dict[str, int] = {}
    archive_status_counts: dict[str, int] = {}
    result_status_counts: dict[str, int] = {}
    tracks_by_worker: dict[str, list[dict]] = {}
    summary = {
        "trackedWorkers": 0,
        "currentWorktrees": 0,
        "stageWorktrees": 0,
        "restoredWorktrees": 0,
        "archivedSnapshots": 0,
        "reportedWorktrees": 0,
        "supersededWorktrees": 0,
        "acceptedEntries": 0,
    }

    for archive in sorted(projection.archives.values(), key=lambda item: item.created_at):
        for snapshot in archive.workspaces:
            summary["archivedSnapshots"] += 1
            _increment(archive_status_counts, archive.status.value)
            result = json.loads(snapshot.result.model_dump_json()) if snapshot.result else {}
            if result.get("status"):
                summary["acceptedEntries"] += 1
                _increment(result_status_counts, result["status"])
            tracks_by_worker.setdefault(snapshot.worker_name, []).append(
                {
                    "entryId": worktree_entry_id(
                        worker_name=snapshot.worker_name,
                        kind="archived",
                        stage_id=snapshot.stage_id,
                        archive_id=archive.id,
                        workspace_path=snapshot.workspace_path,
                        head_sha=snapshot.head_sha,
                        recorded_at=archive.created_at,
                    ),
                    "workerName": snapshot.worker_name,
                    "kind": "archived",
                    "status": archive.status.value,
                    "stageId": snapshot.stage_id,
                    "archiveId": archive.id,
                    "archiveLabel": archive.label,
                    "recordedAt": archive.created_at,
                    "workspacePath": snapshot.workspace_path,
                    "relativePath": _relative_workspace_path(projection.run.id, snapshot.workspace_path),
                    "branch": snapshot.branch,
                    "baseRef": snapshot.base_ref,
                    "headSha": snapshot.head_sha,
                    **_result_view(result),
                }
            )

    for worker in sorted(projection.workers.values(), key=lambda item: item.started_at):
        summary["currentWorktrees"] += 1
        if "/restored/" in worker.workspace_path:
            summary["restoredWorktrees"] += 1
            kind = "restored"
        else:
            summary["stageWorktrees"] += 1
            kind = "current"
        if worker.status.value == "superseded":
            summary["supersededWorktrees"] += 1
        if worker.result is not None:
            summary["reportedWorktrees"] += 1
            summary["acceptedEntries"] += 1
            _increment(result_status_counts, worker.result.status.value)
        _increment(current_status_counts, worker.status.value)
        result = json.loads(worker.result.model_dump_json()) if worker.result else {}
        tracks_by_worker.setdefault(worker.worker_name, []).append(
            {
                "entryId": worktree_entry_id(
                    worker_name=worker.worker_name,
                    kind=kind,
                    stage_id=worker.stage_id,
                    archive_id="",
                    workspace_path=worker.workspace_path,
                    head_sha=worker.head_sha,
                    recorded_at=worker.started_at,
                ),
                "workerName": worker.worker_name,
                "kind": kind,
                "status": worker.status.value,
                "stageId": worker.stage_id,
                "archiveId": "",
                "archiveLabel": "",
                "recordedAt": worker.started_at,
                "workspacePath": worker.workspace_path,
                "relativePath": _relative_workspace_path(projection.run.id, worker.workspace_path),
                "branch": worker.branch,
                "baseRef": worker.base_ref,
                "headSha": worker.head_sha,
                **_result_view(result),
            }
        )

    tracks: list[dict] = []
    for worker_name, entries in sorted(tracks_by_worker.items()):
        ordered = sorted(entries, key=lambda item: (item["recordedAt"], item["kind"]))
        tracks.append(
            {
                "workerName": worker_name,
                "entries": ordered,
            }
        )

    summary["trackedWorkers"] = len(tracks)
    return {
        "summary": summary,
        "currentStatusCounts": current_status_counts,
        "archiveStatusCounts": archive_status_counts,
        "resultStatusCounts": result_status_counts,
        "tracks": tracks,
    }


def _run_summary(projection, *, worktree_track: dict | None = None) -> dict:
    track = worktree_track or _worktree_track(projection)
    pending_gates = [
        gate for gate in projection.approvals.values() if gate.status.value == "pending"
    ]
    workers = [_worker_view(worker) for worker in projection.workers.values()]
    live_workers = sum(1 for worker in workers if worker["status"] in _LIVE_WORKER_STATUSES)
    unhealthy_workers = sum(
        1 for worker in workers if worker["status"] in _UNHEALTHY_WORKER_STATUSES
    )
    return {
        "id": projection.run.id,
        "name": projection.run.name,
        "status": projection.run.status.value,
        "projectProfile": projection.run.project_profile.value,
        "repoRoot": projection.run.repo_root,
        "baseRef": projection.run.base_ref,
        "direction": projection.run.direction,
        "integrationRef": projection.run.integration_ref,
        "maxActiveFeatures": projection.run.max_active_features,
        "createdAt": projection.run.created_at,
        "currentStageId": projection.run.current_stage_id,
        "activePlanId": projection.run.active_plan_id,
        "needsReplan": projection.run.needs_replan,
        "dirtyReason": projection.run.dirty_reason,
        "dirtySince": projection.run.dirty_since,
        "dirtyStageId": projection.run.dirty_stage_id,
        "latestConstraintId": projection.run.latest_constraint_id,
        "latestConstraintAt": projection.run.latest_constraint_at,
        "workers": len(workers),
        "liveWorkers": live_workers,
        "unhealthyWorkers": unhealthy_workers,
        "constraints": len(projection.constraints),
        "archives": len(projection.archives),
        "features": len(projection.features),
        "batches": len(projection.batches),
        "readyFeatureCount": projection.stats.ready_feature_count,
        "openBatchCount": projection.stats.open_batch_count,
        "pendingApprovals": len(pending_gates),
        "openInterventionCount": sum(
            1
            for intervention in projection.interventions.values()
            if intervention.status == InterventionStatus.open
        ),
        "pendingRecovery": projection.stats.pending_recovery_count,
        "currentWorktrees": track["summary"]["currentWorktrees"],
        "restoredWorktrees": track["summary"]["restoredWorktrees"],
        "archivedSnapshots": track["summary"]["archivedSnapshots"],
        "trackedWorkers": track["summary"]["trackedWorkers"],
        "lastEventAt": projection.last_event_at,
        "ownerDataDir": str(get_data_dir().resolve()),
        "daemonPid": next(
            (worker.daemon_pid for worker in projection.workers.values() if worker.daemon_pid > 0),
            0,
        ),
        "activeClaims": _active_claim_summary(projection),
    }


def summarize_runs(service: BranchClawService | None = None) -> list[dict]:
    svc = service or BranchClawService()
    current_data_dir = str(get_data_dir().resolve())
    current_key = data_dir_key(current_data_dir)
    runs = []
    for item in svc.list_runs():
        track = _worktree_track(item)
        summary = _run_summary(item, worktree_track=track)
        summary["dataDirKey"] = current_key
        summary["ownerDataDir"] = current_data_dir
        runs.append(summary)
    runs.sort(key=lambda item: item.get("lastEventAt") or item.get("createdAt") or "", reverse=True)
    return runs


def summarize_run(run_id: str, service: BranchClawService | None = None) -> dict:
    svc = service or BranchClawService()
    current_data_dir = str(get_data_dir().resolve())
    current_key = data_dir_key(current_data_dir)
    projection = svc.get_run(run_id)
    worktree_track = _worktree_track(projection)
    pending_gates = [
        gate for gate in projection.approvals.values() if gate.status.value == "pending"
    ]
    run_payload = _run_summary(projection, worktree_track=worktree_track)
    run_payload["dataDirKey"] = current_key
    run_payload["ownerDataDir"] = current_data_dir
    return {
        "run": run_payload,
        "stages": [json.loads(stage.model_dump_json()) for stage in projection.stages.values()],
        "plans": [json.loads(plan.model_dump_json()) for plan in projection.plans.values()],
        "features": [_feature_view(feature) for feature in projection.features.values()],
        "batches": [_batch_view(batch, projection) for batch in projection.batches.values()],
        "workers": [_worker_view(worker) for worker in projection.workers.values()],
        "archives": [json.loads(archive.model_dump_json()) for archive in projection.archives.values()],
        "approvals": [_approval_view(gate, worktree_track) for gate in pending_gates],
        "interventions": _intervention_views(projection, worktree_track),
        "constraints": [json.loads(item.model_dump_json()) for item in projection.constraints],
        "stats": json.loads(projection.stats.model_dump_json()),
        "worktreeTrack": worktree_track,
        "lastEventAt": projection.last_event_at,
    }


class StandaloneDashboardBackend:
    def __init__(self, service: BranchClawService | None = None) -> None:
        self.service = service or BranchClawService()
        self._dashboard_url = ""
        self._data_dir = str(get_data_dir().resolve())

    def bind(self, host: str, port: int) -> None:
        self._dashboard_url = f"http://{host}:{port}"

    def current_data_dir_key(self) -> str:
        return data_dir_key(self._data_dir)

    def daemon_status(self) -> dict:
        return {
            "running": False,
            "daemon_pid": 0,
            "socket_path": "",
            "started_at": "",
            "dashboard_running": True,
            "dashboard_host": self._dashboard_url.split("://", 1)[-1].split(":", 1)[0] if self._dashboard_url else "",
            "dashboard_port": int(self._dashboard_url.rsplit(":", 1)[-1]) if ":" in self._dashboard_url else 0,
            "dashboard_url": self._dashboard_url,
            "data_dirs": [
                {
                    "dataDirKey": self.current_data_dir_key(),
                    "dataDir": self._data_dir,
                    "runCount": len(self.runs()),
                    "processCount": 0,
                    "liveWorkers": sum(item.get("liveWorkers", 0) for item in self.runs()),
                    "unhealthyWorkers": sum(item.get("unhealthyWorkers", 0) for item in self.runs()),
                    "pendingApprovals": sum(item.get("pendingApprovals", 0) for item in self.runs()),
                }
            ],
            "processes": [],
        }

    def data_dirs(self) -> list[dict]:
        return self.daemon_status()["data_dirs"]

    def processes(self) -> list[dict]:
        return []

    def runs(self) -> list[dict]:
        runs = summarize_runs(self.service)
        for item in runs:
            item["dataDirKey"] = self.current_data_dir_key()
            item["ownerDataDir"] = self._data_dir
        return runs

    def run(self, data_dir_key_value: str, run_id: str) -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        payload = summarize_run(run_id, self.service)
        payload["run"]["dataDirKey"] = self.current_data_dir_key()
        payload["run"]["ownerDataDir"] = self._data_dir
        return payload

    def recent_events(self, data_dir_key_value: str, run_id: str, *, limit: int = 20) -> list[dict]:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        events = EventStore().list_events(run_id)
        rows: list[dict] = []
        for item in events:
            if item.event_type == "worker.heartbeat":
                continue
            payload = item.model_dump(mode="json")
            payload["level"] = _event_level(item.event_type, item.payload)
            rows.append(payload)
        return rows[-max(1, limit):]

    def stop_worker(self, data_dir_key_value: str, run_id: str, worker_name: str) -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        self.service.stop_worker(run_id, worker_name)
        return {"requested": True, "workerName": worker_name}

    def restart_worker(self, data_dir_key_value: str, run_id: str, worker_name: str) -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        worker = self.service.restart_worker(run_id, worker_name)
        return {
            "requested": True,
            "workerName": worker_name,
            "status": worker.status.value,
        }

    def reconcile_run(self, data_dir_key_value: str, run_id: str) -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        projection = self.service.reconcile_workers(run_id)
        return {"runId": run_id, "pendingRecovery": projection.stats.pending_recovery_count}

    def approve_gate(self, data_dir_key_value: str, run_id: str, gate_id: str, *, actor: str = "", feedback: str = "") -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        projection = self.service.approve_gate(run_id, gate_id, actor=actor, feedback=feedback)
        return {"approved": True, "gateId": gate_id, "runStatus": projection.run.status.value}

    def reject_gate(self, data_dir_key_value: str, run_id: str, gate_id: str, *, actor: str = "", feedback: str = "") -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        projection = self.service.reject_gate(run_id, gate_id, actor=actor, feedback=feedback)
        return {"rejected": True, "gateId": gate_id, "runStatus": projection.run.status.value}

    def create_archive(self, data_dir_key_value: str, run_id: str, *, label: str = "", summary: str = "", actor: str = "") -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        archive, gate = self.service.create_archive(run_id, label=label, summary=summary, actor=actor)
        return {"archiveId": archive.id, "gateId": gate.id, "status": archive.status.value}

    def request_restore(self, data_dir_key_value: str, run_id: str, archive_id: str, *, actor: str = "") -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        gate = self.service.request_restore(run_id, archive_id, actor=actor)
        return {"archiveId": archive_id, "gateId": gate.id, "status": gate.status.value}

    def request_merge(
        self,
        data_dir_key_value: str,
        run_id: str,
        *,
        archive_id: str = "",
        batch_id: str = "",
        actor: str = "",
    ) -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        gate = self.service.request_merge(run_id, archive_id=archive_id, batch_id=batch_id, actor=actor)
        return {
            "archiveId": archive_id,
            "batchId": batch_id,
            "gateId": gate.id,
            "status": gate.status.value,
        }

    def request_promote(self, data_dir_key_value: str, run_id: str, *, batch_id: str, actor: str = "") -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        gate = self.service.request_promote(run_id, batch_id=batch_id, actor=actor)
        return {"batchId": batch_id, "gateId": gate.id, "status": gate.status.value}

    def create_run(
        self,
        *,
        data_dir_key_value: str = "",
        data_dir: str = "",
        repo: str,
        name: str,
        description: str = "",
        project_profile: str = "backend",
        spec_content: str = "",
        rules_content: str = "",
        direction: str = "",
        integration_ref: str = "",
        max_active_features: int = 2,
        initial_plan: str = "",
        author: str = "",
    ) -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        if data_dir and str(Path(data_dir).expanduser().resolve()) != self._data_dir:
            raise RuntimeError("Standalone dashboard can only create runs in its current data dir")
        projection = self.service.create_run(
            name,
            description=description,
            project_profile=project_profile,
            spec_content=spec_content,
            rules_content=rules_content,
            repo=repo,
            direction=direction,
            integration_ref=integration_ref,
            max_active_features=max_active_features,
        )
        plan, gate = self.service.propose_plan(
            projection.run.id,
            initial_plan,
            summary="Initial dashboard plan",
            author=author,
        )
        refreshed = self.service.get_run(projection.run.id, rebuild=True)
        return {
            "dataDirKey": self.current_data_dir_key(),
            "ownerDataDir": self._data_dir,
            "runId": projection.run.id,
            "planId": plan.id,
            "gateId": gate.id,
            "runStatus": refreshed.run.status.value,
        }

    def create_workspace(
        self,
        data_dir_key_value: str,
        run_id: str,
        *,
        worker_name: str,
        task: str = "",
        backend: str = "tmux",
        command: str | list[str] | None = None,
        feature_id: str = "",
        skip_permissions: bool | None = None,
    ) -> dict:
        if data_dir_key_value and data_dir_key_value != self.current_data_dir_key():
            raise RuntimeError(f"Unknown data dir key '{data_dir_key_value}'")
        final_command = [str(item) for item in command] if isinstance(command, list) else shlex.split(str(command or "").strip())
        if not final_command:
            final_command = ["claude"]
        worker = self.service.spawn_worker(
            run_id,
            worker_name,
            command=final_command,
            backend=backend or "tmux",
            task=task,
            feature_id=feature_id,
            skip_permissions=skip_permissions,
        )
        return {
            "requested": True,
            "dataDirKey": self.current_data_dir_key(),
            "runId": run_id,
            "workerName": worker.worker_name,
            "status": worker.status.value,
            "workspacePath": worker.workspace_path,
        }


class DaemonDashboardBackend:
    def __init__(self, controller) -> None:
        self.controller = controller

    def bind(self, host: str, port: int) -> None:
        return None

    def current_data_dir_key(self) -> str:
        data_dirs = self.controller.dashboard_data_dirs_payload()
        return data_dirs[0]["dataDirKey"] if data_dirs else ""

    def daemon_status(self) -> dict:
        return self.controller.dashboard_status_payload()

    def data_dirs(self) -> list[dict]:
        return self.controller.dashboard_data_dirs_payload()

    def processes(self) -> list[dict]:
        return self.controller.dashboard_processes_payload()

    def runs(self) -> list[dict]:
        return self.controller.dashboard_runs_payload()

    def run(self, data_dir_key_value: str, run_id: str) -> dict:
        return self.controller.dashboard_run_payload(data_dir_key_value, run_id)

    def recent_events(self, data_dir_key_value: str, run_id: str, *, limit: int = 20) -> list[dict]:
        return self.controller.dashboard_recent_events_payload(data_dir_key_value, run_id, limit=limit)

    def stop_worker(self, data_dir_key_value: str, run_id: str, worker_name: str) -> dict:
        return self.controller.dashboard_stop_worker(data_dir_key_value, run_id, worker_name)

    def restart_worker(self, data_dir_key_value: str, run_id: str, worker_name: str) -> dict:
        return self.controller.dashboard_restart_worker(data_dir_key_value, run_id, worker_name)

    def reconcile_run(self, data_dir_key_value: str, run_id: str) -> dict:
        return self.controller.dashboard_reconcile_run(data_dir_key_value, run_id)

    def approve_gate(self, data_dir_key_value: str, run_id: str, gate_id: str, *, actor: str = "", feedback: str = "") -> dict:
        return self.controller.dashboard_approve_gate(data_dir_key_value, run_id, gate_id, actor=actor, feedback=feedback)

    def reject_gate(self, data_dir_key_value: str, run_id: str, gate_id: str, *, actor: str = "", feedback: str = "") -> dict:
        return self.controller.dashboard_reject_gate(data_dir_key_value, run_id, gate_id, actor=actor, feedback=feedback)

    def create_archive(self, data_dir_key_value: str, run_id: str, *, label: str = "", summary: str = "", actor: str = "") -> dict:
        return self.controller.dashboard_create_archive(data_dir_key_value, run_id, label=label, summary=summary, actor=actor)

    def request_restore(self, data_dir_key_value: str, run_id: str, archive_id: str, *, actor: str = "") -> dict:
        return self.controller.dashboard_request_restore(data_dir_key_value, run_id, archive_id, actor=actor)

    def request_merge(
        self,
        data_dir_key_value: str,
        run_id: str,
        *,
        archive_id: str = "",
        batch_id: str = "",
        actor: str = "",
    ) -> dict:
        return self.controller.dashboard_request_merge(
            data_dir_key_value,
            run_id,
            archive_id=archive_id,
            batch_id=batch_id,
            actor=actor,
        )

    def request_promote(self, data_dir_key_value: str, run_id: str, *, batch_id: str, actor: str = "") -> dict:
        return self.controller.dashboard_request_promote(data_dir_key_value, run_id, batch_id=batch_id, actor=actor)

    def create_run(
        self,
        *,
        data_dir_key_value: str = "",
        data_dir: str = "",
        repo: str,
        name: str,
        description: str = "",
        project_profile: str = "backend",
        spec_content: str = "",
        rules_content: str = "",
        direction: str = "",
        integration_ref: str = "",
        max_active_features: int = 2,
        initial_plan: str = "",
        author: str = "",
    ) -> dict:
        return self.controller.dashboard_create_run(
            data_dir_key=data_dir_key_value,
            data_dir=data_dir,
            repo=repo,
            name=name,
            description=description,
            project_profile=project_profile,
            spec_content=spec_content,
            rules_content=rules_content,
            direction=direction,
            integration_ref=integration_ref,
            max_active_features=max_active_features,
            initial_plan=initial_plan,
            author=author,
        )

    def create_workspace(
        self,
        data_dir_key_value: str,
        run_id: str,
        *,
        worker_name: str,
        task: str = "",
        backend: str = "tmux",
        command: str | list[str] | None = None,
        feature_id: str = "",
        skip_permissions: bool | None = None,
    ) -> dict:
        return self.controller.dashboard_spawn_worker(
            data_dir_key_value,
            run_id,
            worker_name=worker_name,
            task=task,
            backend=backend,
            command=command,
            feature_id=feature_id,
            skip_permissions=skip_permissions,
        )


_BOARD_STATIC_DIR = Path(__file__).with_name("board_static")
_BOARD_PAGE_FILES = {
    "picker": Path("pages/picker.html"),
    "workspace": Path("pages/workspace.html"),
    "review": Path("pages/review.html"),
    "control-plane": Path("pages/control-plane.html"),
}


def _board_static_file(relative_path: str | Path) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Invalid dashboard asset path: {relative_path}")
    static_root = _BOARD_STATIC_DIR.resolve()
    candidate = (_BOARD_STATIC_DIR / relative).resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError as exc:
        raise RuntimeError(f"Invalid dashboard asset path: {relative_path}") from exc
    if not candidate.exists() or not candidate.is_file():
        raise RuntimeError(f"Dashboard asset not found: {relative_path}")
    return candidate


def _read_board_static_text(relative_path: str | Path) -> str:
    return _board_static_file(relative_path).read_text(encoding="utf-8")


def _render_page_templates() -> str:
    rendered_templates: list[str] = []
    for page_name, relative_path in _BOARD_PAGE_FILES.items():
        page_html = _read_board_static_text(relative_path)
        rendered_templates.append(
            f'        <template data-page-template="{page_name}">\n'
            f"{page_html}\n"
            "        </template>"
        )
    return "\n".join(rendered_templates)


def _render_page_html(page_name: str) -> str:
    page_relative = _BOARD_PAGE_FILES.get(page_name)
    if page_relative is None:
        raise RuntimeError(f"Unknown dashboard page: {page_name}")
    base_html = _read_board_static_text("base.html")
    page_html = _read_board_static_text(page_relative)
    return (
        base_html
        .replace("{{PAGE_ID}}", page_name)
        .replace("{{PAGE_CONTENT}}", page_html)
        .replace("{{PAGE_TEMPLATES}}", _render_page_templates())
    )


def _static_content_type(path: Path) -> str:
    if path.suffix == ".css":
        return "text/css; charset=utf-8"
    if path.suffix == ".js":
        return "application/javascript; charset=utf-8"
    if path.suffix == ".html":
        return "text/html; charset=utf-8"
    return "application/octet-stream"


def _error_body(message: str) -> bytes:
    return json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")


class _DashboardHandler(BaseHTTPRequestHandler):
    backend = StandaloneDashboardBackend()
    poll_interval = 2.0

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path in {"/", "/index.html"}:
                self._serve_page("picker")
                return
            if path == "/workspace.html":
                self._serve_page("workspace")
                return
            if path == "/review.html":
                self._serve_page("review")
                return
            if path == "/control-plane.html":
                self._serve_page("control-plane")
                return
            if path.startswith("/static/"):
                self._serve_static_asset(path[len("/static/"):])
                return
            if path == "/api/daemon/status":
                self._serve_json(self.backend.daemon_status())
                return
            if path == "/api/data-dirs":
                self._serve_json(self.backend.data_dirs())
                return
            if path == "/api/processes":
                self._serve_json(self.backend.processes())
                return
            if path == "/api/runs":
                self._serve_json(self.backend.runs())
                return
            if path.startswith("/api/data-dirs/"):
                self._handle_scoped_get(path, parsed.query)
                return
            if path.startswith("/api/run/"):
                run_id = path[len("/api/run/"):].strip("/")
                self._serve_json(self.backend.run(self.backend.current_data_dir_key(), run_id))
                return
            if path.startswith("/api/events/"):
                run_id = path[len("/api/events/"):].strip("/")
                self._serve_sse(self.backend.current_data_dir_key(), run_id)
                return
            self.send_error(404)
        except Exception as exc:  # noqa: BLE001
            self._serve_error(str(exc), status=400)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/runs":
                payload = self._read_json_body()
                self._serve_json(
                    self.backend.create_run(
                        data_dir_key_value=str(payload.get("dataDirKey", "")),
                        data_dir=str(payload.get("dataDir", "")),
                        repo=str(payload.get("repo", "")),
                        name=str(payload.get("name", "")),
                        description=str(payload.get("description", "")),
                        project_profile=str(payload.get("projectProfile", "backend")),
                        spec_content=str(payload.get("specContent", "")),
                        rules_content=str(payload.get("rulesContent", "")),
                        direction=str(payload.get("direction", "")),
                        integration_ref=str(payload.get("integrationRef", "")),
                        max_active_features=int(payload.get("maxActiveFeatures", 2) or 2),
                        initial_plan=str(payload.get("initialPlan", "")),
                        author=str(payload.get("author", "")),
                    )
                )
                return
            if not path.startswith("/api/data-dirs/"):
                self.send_error(404)
                return
            payload = self._read_json_body()
            self._handle_scoped_post(path, payload)
        except Exception as exc:  # noqa: BLE001
            self._serve_error(str(exc), status=400)

    def _handle_scoped_get(self, path: str, query: str):
        parts = [part for part in path.split("/") if part]
        if len(parts) < 5 or parts[0] != "api" or parts[1] != "data-dirs":
            self.send_error(404)
            return
        data_dir_key_value = parts[2]
        if parts[3] == "runs" and len(parts) == 5:
            self._serve_json(self.backend.run(data_dir_key_value, parts[4]))
            return
        if parts[3] == "runs" and len(parts) == 6 and parts[5] == "recent-events":
            limit = int(parse_qs(query).get("limit", ["20"])[0] or "20")
            self._serve_json(self.backend.recent_events(data_dir_key_value, parts[4], limit=limit))
            return
        if parts[3] == "events" and len(parts) == 5:
            self._serve_sse(data_dir_key_value, parts[4])
            return
        self.send_error(404)

    def _handle_scoped_post(self, path: str, payload: dict):
        parts = [part for part in path.split("/") if part]
        if len(parts) < 5 or parts[0] != "api" or parts[1] != "data-dirs":
            self.send_error(404)
            return
        data_dir_key_value = parts[2]
        if parts[3] != "runs":
            self.send_error(404)
            return
        run_id = parts[4]
        if len(parts) == 6 and parts[5] == "reconcile":
            self._serve_json(self.backend.reconcile_run(data_dir_key_value, run_id))
            return
        if len(parts) == 6 and parts[5] == "workers":
            self._serve_json(
                self.backend.create_workspace(
                    data_dir_key_value,
                    run_id,
                    worker_name=str(payload.get("workerName", "")),
                    task=str(payload.get("task", "")),
                    backend=str(payload.get("backend", "tmux")),
                    command=payload.get("command", ""),
                    feature_id=str(payload.get("featureId", "")),
                    skip_permissions=payload.get("skipPermissions"),
                )
            )
            return
        if len(parts) == 8 and parts[5] == "workers" and parts[7] == "stop":
            self._serve_json(self.backend.stop_worker(data_dir_key_value, run_id, parts[6]))
            return
        if len(parts) == 8 and parts[5] == "workers" and parts[7] == "restart":
            self._serve_json(self.backend.restart_worker(data_dir_key_value, run_id, parts[6]))
            return
        if len(parts) == 8 and parts[5] == "gates" and parts[7] == "approve":
            self._serve_json(
                self.backend.approve_gate(
                    data_dir_key_value,
                    run_id,
                    parts[6],
                    actor=str(payload.get("actor", "")),
                    feedback=str(payload.get("feedback", "")),
                )
            )
            return
        if len(parts) == 8 and parts[5] == "gates" and parts[7] == "reject":
            self._serve_json(
                self.backend.reject_gate(
                    data_dir_key_value,
                    run_id,
                    parts[6],
                    actor=str(payload.get("actor", "")),
                    feedback=str(payload.get("feedback", "")),
                )
            )
            return
        if len(parts) == 6 and parts[5] == "archives":
            self._serve_json(
                self.backend.create_archive(
                    data_dir_key_value,
                    run_id,
                    label=str(payload.get("label", "")),
                    summary=str(payload.get("summary", "")),
                    actor=str(payload.get("actor", "")),
                )
            )
            return
        if len(parts) == 8 and parts[5] == "archives" and parts[7] == "restore":
            self._serve_json(
                self.backend.request_restore(
                    data_dir_key_value,
                    run_id,
                    parts[6],
                    actor=str(payload.get("actor", "")),
                )
            )
            return
        if len(parts) == 6 and parts[5] == "merge-request":
            self._serve_json(
                self.backend.request_merge(
                    data_dir_key_value,
                    run_id,
                    archive_id=str(payload.get("archiveId", "")),
                    batch_id=str(payload.get("batchId", "")),
                    actor=str(payload.get("actor", "")),
                )
            )
            return
        if len(parts) == 6 and parts[5] == "promote-request":
            self._serve_json(
                self.backend.request_promote(
                    data_dir_key_value,
                    run_id,
                    batch_id=str(payload.get("batchId", "")),
                    actor=str(payload.get("actor", "")),
                )
            )
            return
        self.send_error(404)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("JSON body must be an object")
        return payload

    def _serve_page(self, page_name: str):
        content = _render_page_html(page_name).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_static_asset(self, relative_path: str):
        asset_path = _board_static_file(relative_path)
        content = asset_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _static_content_type(asset_path))
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self, data, *, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_error(self, message: str, *, status: int = 400):
        body = _error_body(message)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self, data_dir_key_value: str, run_id: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                payload = json.dumps(self.backend.run(data_dir_key_value, run_id), ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(self.poll_interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, format, *args):
        first = str(args[0]) if args else ""
        if "/api/data-dirs/" not in first or "/events/" not in first:
            super().log_message(format, *args)


def build_server(
    host: str = "127.0.0.1",
    port: int = 8090,
    poll_interval: float = 2.0,
    service: BranchClawService | None = None,
    backend=None,
) -> ThreadingHTTPServer:
    """Build a configurable dashboard server for production or tests."""
    resolved_backend = backend or StandaloneDashboardBackend(service=service)
    handler_cls = type("BranchClawDashboardHandler", (_DashboardHandler,), {})
    handler_cls.poll_interval = poll_interval
    handler_cls.backend = resolved_backend
    server = ThreadingHTTPServer((host, port), handler_cls)
    actual_host, actual_port = server.server_address
    if hasattr(resolved_backend, "bind"):
        resolved_backend.bind(str(actual_host), int(actual_port))
    return server


def serve(host: str = "127.0.0.1", port: int = 8090, poll_interval: float = 2.0) -> None:
    """Serve a BranchClaw dashboard over HTTP."""
    server = build_server(host=host, port=port, poll_interval=poll_interval)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
