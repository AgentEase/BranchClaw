"""Event storage and projection rebuilding for BranchClaw."""

from __future__ import annotations

import fcntl
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from branchclaw.config import get_data_dir
from branchclaw.models import (
    ApprovalGate,
    ApprovalStatus,
    ApprovalType,
    ArchiveStatus,
    BatchRecord,
    BatchStatus,
    ConstraintPatch,
    EventRecord,
    FeatureRecord,
    FeatureStatus,
    InterventionStatus,
    PlanProposal,
    PlanStatus,
    ProjectionStats,
    RunProjection,
    RunRecord,
    RunStatus,
    StageArchive,
    StageRecord,
    StageStatus,
    ValidationStatus,
    WorkerIntervention,
    WorkerResult,
    WorkerRuntime,
    WorkerStatus,
)


def runs_root() -> Path:
    path = get_data_dir() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_dir(run_id: str) -> Path:
    path = runs_root() / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def events_dir(run_id: str) -> Path:
    path = run_dir(run_id) / "events"
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifacts_dir(run_id: str) -> Path:
    path = run_dir(run_id) / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def projection_path(run_id: str) -> Path:
    return run_dir(run_id) / "projection.json"


def run_lock_path(run_id: str) -> Path:
    return run_dir(run_id) / ".events.lock"


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        Path(tmp_name).replace(path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


@contextmanager
def event_lock(run_id: str):
    path = run_lock_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _feature_by_worker(projection: RunProjection, worker_name: str) -> FeatureRecord | None:
    for feature in projection.features.values():
        if feature.worker_name == worker_name:
            return feature
    return None


def _batch_by_id(projection: RunProjection, batch_id: str) -> BatchRecord | None:
    return projection.batches.get(batch_id)


class EventStore:
    """Append-only event storage with cached projections."""

    def append(self, run_id: str, event_type: str, payload: dict[str, Any]) -> EventRecord:
        with event_lock(run_id):
            current = self.list_events(run_id)
            sequence = len(current) + 1
            event = EventRecord(
                id=f"evt-{sequence:06d}",
                run_id=run_id,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
            )
            path = events_dir(run_id) / f"evt-{sequence:06d}-{event.id}.json"
            save_json(path, json.loads(event.model_dump_json()))
            projection = project_events(current + [event])
            save_json(projection_path(run_id), json.loads(projection.model_dump_json()))
            return event

    def list_events(self, run_id: str) -> list[EventRecord]:
        items: list[EventRecord] = []
        for file_path in sorted(events_dir(run_id).glob("evt-*.json")):
            try:
                items.append(
                    EventRecord.model_validate(
                        json.loads(file_path.read_text(encoding="utf-8"))
                    )
                )
            except Exception:
                continue
        return items

    def export(self, run_id: str, *, include_heartbeats: bool = False) -> dict[str, Any]:
        events = self.list_events(run_id)
        projection = project_events(events)
        exported_events = events if include_heartbeats else [
            event for event in events if event.event_type != "worker.heartbeat"
        ]
        return {
            "runId": run_id,
            "events": [json.loads(event.model_dump_json()) for event in exported_events],
            "projection": json.loads(projection.model_dump_json()),
        }

    def load_projection(self, run_id: str, rebuild: bool = False) -> RunProjection:
        path = projection_path(run_id)
        if not rebuild and path.exists():
            try:
                return RunProjection.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
        projection = project_events(self.list_events(run_id))
        save_json(path, json.loads(projection.model_dump_json()))
        return projection

    def list_runs(self) -> list[RunProjection]:
        projections: list[RunProjection] = []
        for path in sorted(runs_root().iterdir()):
            if not path.is_dir():
                continue
            try:
                projections.append(self.load_projection(path.name))
            except ValueError:
                continue
        return projections


def project_events(events: list[EventRecord]) -> RunProjection:
    if not events:
        raise ValueError("No events found for run")

    projection: RunProjection | None = None
    for event in sorted(events, key=lambda item: item.sequence):
        payload = event.payload
        if event.event_type == "run.created":
            projection = RunProjection(run=RunRecord.model_validate(payload["run"]))
        elif projection is None:
            raise ValueError("Run projection missing run.created")
        elif event.event_type == "stage.created":
            stage = StageRecord.model_validate(payload["stage"])
            projection.stages[stage.id] = stage
            projection.run.current_stage_id = stage.id
        elif event.event_type == "plan.proposed":
            plan = PlanProposal.model_validate(payload["plan"])
            projection.plans[plan.id] = plan
            projection.run.active_plan_id = plan.id
            projection.run.status = RunStatus.awaiting_plan_approval
        elif event.event_type == "plan.superseded":
            plan = projection.plans.get(payload["plan_id"])
            if plan:
                plan.status = PlanStatus.superseded
                plan.updated_at = event.timestamp
        elif event.event_type == "plan.replan_requested":
            projection.run.needs_replan = True
            projection.run.dirty_reason = payload.get("reason", "constraint")
            projection.run.dirty_since = event.timestamp
            projection.run.dirty_stage_id = payload.get("stage_id", projection.run.current_stage_id)
            projection.run.latest_constraint_id = payload.get("constraint_id", "")
            projection.run.latest_constraint_at = payload.get("constraint_at", event.timestamp)
        elif event.event_type == "plan.replan_cleared":
            projection.run.needs_replan = False
            projection.run.dirty_reason = ""
            projection.run.dirty_since = ""
            projection.run.dirty_stage_id = ""
            projection.run.latest_constraint_id = ""
            projection.run.latest_constraint_at = ""
        elif event.event_type == "approval.requested":
            gate = ApprovalGate.model_validate(payload["gate"])
            projection.approvals[gate.id] = gate
            if gate.gate_type == ApprovalType.plan:
                projection.run.status = RunStatus.awaiting_plan_approval
            elif gate.gate_type == ApprovalType.archive:
                projection.run.status = RunStatus.awaiting_archive_approval
            elif gate.gate_type in {ApprovalType.merge, ApprovalType.promote} and gate.target_id.startswith("batch-"):
                projection.run.status = RunStatus.executing
            else:
                projection.run.status = RunStatus.awaiting_merge_or_rollback
        elif event.event_type == "approval.approved":
            gate = projection.approvals[payload["gate_id"]]
            gate.status = ApprovalStatus.approved
            gate.actor = payload.get("actor", "")
            gate.feedback = payload.get("feedback", "")
            gate.resolved_at = event.timestamp
            if gate.gate_type == ApprovalType.plan:
                plan = projection.plans[gate.target_id]
                plan.status = PlanStatus.approved
                plan.feedback = gate.feedback
                plan.updated_at = event.timestamp
                projection.run.active_plan_id = plan.id
                projection.run.status = RunStatus.executing
                stage = projection.stages.get(plan.stage_id)
                if stage:
                    stage.status = StageStatus.executing
                    stage.updated_at = event.timestamp
                    projection.run.current_stage_id = stage.id
            elif gate.gate_type == ApprovalType.archive:
                archive = projection.archives[gate.target_id]
                archive.status = ArchiveStatus.approved
                stage = projection.stages.get(archive.stage_id)
                if stage:
                    stage.status = StageStatus.archived
                    stage.updated_at = event.timestamp
                projection.run.status = RunStatus.archived
            elif gate.gate_type == ApprovalType.merge:
                if gate.target_id.startswith("batch-"):
                    projection.run.status = RunStatus.executing
                else:
                    projection.run.status = RunStatus.awaiting_merge_or_rollback
            elif gate.gate_type == ApprovalType.promote:
                projection.run.status = RunStatus.executing
            elif gate.gate_type == ApprovalType.rollback:
                projection.run.status = RunStatus.awaiting_merge_or_rollback
        elif event.event_type == "approval.rejected":
            gate = projection.approvals[payload["gate_id"]]
            gate.status = ApprovalStatus.rejected
            gate.actor = payload.get("actor", "")
            gate.feedback = payload.get("feedback", "")
            gate.resolved_at = event.timestamp
            if gate.gate_type == ApprovalType.plan:
                plan = projection.plans[gate.target_id]
                plan.status = PlanStatus.rejected
                plan.feedback = gate.feedback
                plan.updated_at = event.timestamp
                projection.run.status = RunStatus.draft_plan
            elif gate.gate_type == ApprovalType.archive:
                projection.run.status = RunStatus.executing
            elif gate.gate_type in {ApprovalType.merge, ApprovalType.promote} and gate.target_id.startswith("batch-"):
                batch = projection.batches.get(gate.target_id)
                if batch:
                    batch.status = BatchStatus.rejected
                projection.run.status = RunStatus.executing
            else:
                projection.run.status = RunStatus.archived
        elif event.event_type == "constraint.added":
            constraint = ConstraintPatch.model_validate(payload["constraint"])
            projection.constraints.append(constraint)
        elif event.event_type == "feature.created":
            feature = FeatureRecord.model_validate(payload["feature"])
            projection.features[feature.id] = feature
        elif event.event_type == "feature.assigned":
            feature = projection.features.get(payload["feature_id"])
            if feature:
                feature.worker_name = payload.get("worker_name", feature.worker_name)
                feature.status = FeatureStatus.assigned
                feature.updated_at = event.timestamp
        elif event.event_type == "feature.updated":
            feature = FeatureRecord.model_validate(payload["feature"])
            projection.features[feature.id] = feature
        elif event.event_type == "feature.ready":
            feature = FeatureRecord.model_validate(payload["feature"])
            projection.features[feature.id] = feature
        elif event.event_type == "feature.blocked":
            feature = FeatureRecord.model_validate(payload["feature"])
            projection.features[feature.id] = feature
        elif event.event_type == "batch.proposed":
            batch = BatchRecord.model_validate(payload["batch"])
            projection.batches[batch.id] = batch
            for feature_id in batch.feature_ids:
                feature = projection.features.get(feature_id)
                if feature and feature.status != FeatureStatus.merged:
                    feature.status = FeatureStatus.batched
                    feature.updated_at = event.timestamp
        elif event.event_type == "batch.approved":
            batch = projection.batches.get(payload["batch_id"])
            if batch:
                batch.status = BatchStatus.integrating
                batch.approved_at = payload.get("approved_at", event.timestamp)
        elif event.event_type == "batch.integration_validated":
            batch = projection.batches.get(payload["batch_id"])
            if batch:
                batch.status = BatchStatus.pending_promote
                batch.validation_status = ValidationStatus.passed
                batch.validation_command = payload.get("validation_command", batch.validation_command)
                batch.validation_output = payload.get("validation_output", batch.validation_output)
        elif event.event_type == "batch.integration_failed":
            batch = projection.batches.get(payload["batch_id"])
            if batch:
                batch.status = BatchStatus.integration_failed
                batch.validation_status = ValidationStatus.failed
                batch.validation_command = payload.get("validation_command", batch.validation_command)
                batch.validation_output = payload.get("validation_output", batch.validation_output)
                blocker = payload.get("blocker", "")
                for feature_id in batch.feature_ids:
                    feature = projection.features.get(feature_id)
                    if feature and feature.status != FeatureStatus.merged:
                        feature.status = FeatureStatus.ready
                        feature.integration_blocker = blocker
                        feature.updated_at = event.timestamp
        elif event.event_type == "batch.promoted":
            batch = projection.batches.get(payload["batch_id"])
            if batch:
                batch.status = BatchStatus.completed
                batch.promoted_at = payload.get("promoted_at", event.timestamp)
                batch.validation_status = ValidationStatus.passed
                for feature_id in batch.feature_ids:
                    feature = projection.features.get(feature_id)
                    if feature:
                        feature.status = FeatureStatus.merged
                        feature.integration_blocker = ""
                        feature.updated_at = event.timestamp
        elif event.event_type == "planner.resumed":
            status = payload.get("status")
            if status:
                try:
                    projection.run.status = RunStatus(status)
                except ValueError:
                    pass
        elif event.event_type in {"worker.started", "worker.spawned"}:
            worker = WorkerRuntime.model_validate(payload["worker"])
            projection.workers[worker.worker_name] = worker
            if worker.feature_id:
                feature = projection.features.get(worker.feature_id)
                if feature:
                    feature.worker_name = worker.worker_name
                    feature.status = FeatureStatus.assigned
                    feature.snapshot_branch = worker.branch
                    feature.snapshot_workspace_path = worker.workspace_path
                    feature.updated_at = event.timestamp
        elif event.event_type == "worker.heartbeat":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                if worker.status != WorkerStatus.blocked:
                    worker.status = WorkerStatus.running
                worker.heartbeat_at = payload.get("last_heartbeat_at", event.timestamp)
                worker.last_heartbeat_at = payload.get("last_heartbeat_at", event.timestamp)
                worker.head_sha = payload.get("head_sha", worker.head_sha)
                worker.pid = payload.get("child_pid", worker.pid)
                worker.child_pid = payload.get("child_pid", worker.child_pid)
                worker.supervisor_pid = payload.get("supervisor_pid", worker.supervisor_pid)
                worker.tmux_target = payload.get("tmux_target", worker.tmux_target)
                worker.stale_at = ""
                if worker.feature_id:
                    feature = projection.features.get(worker.feature_id)
                    if feature and feature.status == FeatureStatus.assigned:
                        feature.status = FeatureStatus.in_progress
                        feature.updated_at = event.timestamp
        elif event.event_type == "worker.checkpoint":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.head_sha = payload.get("head_sha", worker.head_sha)
                if worker.feature_id:
                    feature = projection.features.get(worker.feature_id)
                    if feature and feature.status in {FeatureStatus.assigned, FeatureStatus.in_progress}:
                        feature.status = FeatureStatus.in_progress
                        feature.snapshot_head_sha = worker.head_sha
                        feature.updated_at = event.timestamp
        elif event.event_type == "worker.reported":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.result = WorkerResult.model_validate(payload["result"])
                worker.report_source = payload.get("source", worker.report_source or "operator")
                if worker.feature_id:
                    feature = projection.features.get(worker.feature_id)
                    if feature:
                        feature.result = worker.result
                        feature.result_summary = (
                            worker.result.changed_surface_summary
                            or worker.result.output_snippet
                            or worker.result.architecture_summary[:240]
                        )
                        feature.snapshot_branch = worker.branch
                        feature.snapshot_head_sha = worker.head_sha
                        feature.snapshot_workspace_path = worker.workspace_path
                        feature.snapshot_recorded_at = event.timestamp
                        feature.updated_at = event.timestamp
        elif event.event_type == "worker.preview_updated":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                current = worker.result or WorkerResult(project_profile=projection.run.project_profile)
                current.preview_url = payload.get("preview_url", current.preview_url)
                current.backend_url = payload.get("backend_url", current.backend_url)
                current.start_command = payload.get("start_command", current.start_command)
                current.reported_at = payload.get("reported_at", current.reported_at)
                worker.result = current
                if worker.feature_id:
                    feature = projection.features.get(worker.feature_id)
                    if feature and feature.result:
                        feature.result.preview_url = current.preview_url
                        feature.result.backend_url = current.backend_url
                        feature.updated_at = event.timestamp
        elif event.event_type == "worker.tool_called":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.last_tool_name = payload.get("tool_name", worker.last_tool_name)
                worker.last_tool_status = "running"
                worker.last_tool_at = event.timestamp
                worker.last_tool_error = ""
                worker.last_tool_arguments = payload.get("arguments", {}) or {}
        elif event.event_type == "worker.tool_completed":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.last_tool_name = payload.get("tool_name", worker.last_tool_name)
                worker.last_tool_status = "completed"
                worker.last_tool_at = event.timestamp
                worker.last_tool_error = ""
                worker.last_tool_arguments = {}
                worker.tool_retry_count = 0
                worker.last_failed_diff_signature = ""
                worker.remediation_attempt_count = 0
                worker.restart_attempt_count = 0
                worker.last_remediation_action = ""
                worker.last_remediation_status = ""
                worker.last_remediation_at = ""
                worker.intervention_id = ""
                result = payload.get("result") or {}
                if worker.last_tool_name == "service.start_tmux":
                    worker.active_service_target = result.get("target", worker.active_service_target)
                    worker.active_service_log_path = result.get("log_path", worker.active_service_log_path)
                elif worker.last_tool_name == "service.discover_url":
                    worker.discovered_url = result.get("url", worker.discovered_url)
                elif worker.last_tool_name == "worker.report_result":
                    worker.report_source = result.get("report_source", worker.report_source or "agent")
        elif event.event_type == "worker.tool_failed":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                same_failed_tool = (
                    worker.last_tool_status == "failed"
                    and worker.last_tool_name == payload.get("tool_name", worker.last_tool_name)
                )
                worker.last_tool_name = payload.get("tool_name", worker.last_tool_name)
                worker.last_tool_status = "failed"
                worker.last_tool_at = event.timestamp
                worker.last_tool_error = payload.get("error", "")
                worker.last_tool_arguments = payload.get("arguments", {}) or {}
                worker.tool_retry_count = worker.tool_retry_count + 1 if same_failed_tool else 1
                worker.last_failed_diff_signature = payload.get("diff_signature", "")
        elif event.event_type == "worker.remediation_attempted":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.remediation_attempt_count = int(
                    payload.get("remediation_attempts", worker.remediation_attempt_count + 1)
                )
                worker.last_remediation_action = payload.get("action", worker.last_remediation_action)
                worker.last_remediation_status = "attempted"
                worker.last_remediation_at = event.timestamp
        elif event.event_type == "worker.remediation_succeeded":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.remediation_attempt_count = int(
                    payload.get("remediation_attempts", worker.remediation_attempt_count)
                )
                worker.restart_attempt_count = int(
                    payload.get("restart_attempts", worker.restart_attempt_count)
                )
                worker.last_remediation_action = payload.get("action", worker.last_remediation_action)
                worker.last_remediation_status = "succeeded"
                worker.last_remediation_at = event.timestamp
                worker.active_service_target = payload.get(
                    "active_service_target",
                    worker.active_service_target,
                )
                worker.active_service_log_path = payload.get(
                    "active_service_log_path",
                    worker.active_service_log_path,
                )
                worker.discovered_url = payload.get("discovered_url", worker.discovered_url)
        elif event.event_type == "worker.remediation_failed":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.remediation_attempt_count = int(
                    payload.get("remediation_attempts", worker.remediation_attempt_count)
                )
                worker.restart_attempt_count = int(
                    payload.get("restart_attempts", worker.restart_attempt_count)
                )
                worker.last_remediation_action = payload.get("action", worker.last_remediation_action)
                worker.last_remediation_status = "failed"
                worker.last_remediation_at = event.timestamp
        elif event.event_type == "worker.intervention_opened":
            intervention = WorkerIntervention.model_validate(payload["intervention"])
            projection.interventions[intervention.id] = intervention
            worker = projection.workers.get(intervention.worker_name)
            if worker:
                worker.intervention_id = intervention.id
            if intervention.feature_id:
                feature = projection.features.get(intervention.feature_id)
                if feature and feature.status != FeatureStatus.merged:
                    feature.status = FeatureStatus.blocked
                    feature.integration_blocker = intervention.reason
                    feature.updated_at = event.timestamp
        elif event.event_type == "worker.intervention_resolved":
            intervention = projection.interventions.get(payload["intervention_id"])
            if intervention:
                intervention.status = InterventionStatus.resolved
                intervention.resolved_at = payload.get("resolved_at", event.timestamp)
                intervention.resolution_reason = payload.get("resolution_reason", "")
                worker = projection.workers.get(intervention.worker_name)
                if worker and worker.intervention_id == intervention.id:
                    worker.intervention_id = ""
        elif event.event_type == "worker.blocked":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.status = WorkerStatus.blocked
                worker.blocked_at = payload.get("blocked_at", event.timestamp)
                worker.blocked_reason = payload.get("blocked_reason", "")
                worker.failure_reason = worker.blocked_reason or payload.get("blocked_reason", "")
                worker.last_tool_name = payload.get("tool_name", worker.last_tool_name)
                worker.last_tool_status = "failed"
                worker.last_tool_error = payload.get("last_tool_error", worker.last_tool_error)
                worker.tool_retry_count = payload.get("tool_retry_count", worker.tool_retry_count)
                worker.remediation_attempt_count = payload.get(
                    "remediation_attempts",
                    worker.remediation_attempt_count,
                )
                worker.restart_attempt_count = payload.get(
                    "restart_attempts",
                    worker.restart_attempt_count,
                )
                worker.intervention_id = payload.get("intervention_id", worker.intervention_id)
                worker.last_failed_diff_signature = payload.get(
                    "failure_diff_signature",
                    worker.last_failed_diff_signature,
                )
                if worker.feature_id:
                    feature = projection.features.get(worker.feature_id)
                    if feature and feature.status != FeatureStatus.merged:
                        feature.status = FeatureStatus.blocked
                        feature.integration_blocker = worker.blocked_reason or worker.failure_reason
                        feature.updated_at = event.timestamp
        elif event.event_type == "worker.stale":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.status = WorkerStatus.stale
                worker.stale_at = payload.get("stale_at", event.timestamp)
        elif event.event_type == "worker.reconciled":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                status_value = payload.get("status", worker.status.value)
                try:
                    worker.status = WorkerStatus(status_value)
                except ValueError:
                    pass
                worker.head_sha = payload.get("head_sha", worker.head_sha)
                worker.pid = payload.get("child_pid", worker.pid)
                worker.child_pid = payload.get("child_pid", worker.child_pid)
                worker.supervisor_pid = payload.get("supervisor_pid", worker.supervisor_pid)
                worker.tmux_target = payload.get("tmux_target", worker.tmux_target)
                worker.last_heartbeat_at = payload.get(
                    "last_heartbeat_at",
                    worker.last_heartbeat_at or event.timestamp,
                )
                worker.heartbeat_at = worker.last_heartbeat_at
                worker.stale_at = payload.get("stale_at", "")
                worker.exit_code = payload.get("exit_code", worker.exit_code)
                worker.failure_reason = payload.get("failure_reason", worker.failure_reason)
        elif event.event_type == "worker.exited":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.pid = 0
                worker.child_pid = 0
                worker.exit_code = payload.get("exit_code", worker.exit_code)
                worker.finished_at = payload.get("finished_at", event.timestamp)
                worker.tmux_target = payload.get("tmux_target", worker.tmux_target)
                worker.supervisor_pid = payload.get("supervisor_pid", worker.supervisor_pid)
                worker.active_service_target = payload.get("active_service_target", "")
        elif event.event_type == "worker.failed":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.status = WorkerStatus.failed
                worker.pid = 0
                worker.child_pid = 0
                worker.exit_code = payload.get("exit_code", worker.exit_code)
                worker.finished_at = payload.get("finished_at", event.timestamp)
                worker.failure_reason = payload.get("failure_reason", worker.failure_reason)
                worker.tmux_target = payload.get("tmux_target", worker.tmux_target)
                worker.supervisor_pid = payload.get("supervisor_pid", worker.supervisor_pid)
                worker.active_service_target = payload.get("active_service_target", "")
        elif event.event_type == "worker.stopped":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                preserve = payload.get("preserve_status") or worker.status == WorkerStatus.blocked
                worker.status = worker.status if preserve else WorkerStatus.stopped
                worker.pid = 0
                worker.child_pid = 0
                worker.exit_code = payload.get("exit_code", worker.exit_code)
                worker.finished_at = payload.get("finished_at", event.timestamp)
                worker.failure_reason = (
                    worker.failure_reason
                    if preserve and not payload.get("failure_reason")
                    else payload.get("failure_reason", "")
                )
                worker.tmux_target = payload.get("tmux_target", worker.tmux_target)
                worker.supervisor_pid = payload.get("supervisor_pid", worker.supervisor_pid)
                worker.active_service_target = payload.get("active_service_target", "")
                worker.last_heartbeat_at = payload.get(
                    "last_heartbeat_at",
                    worker.last_heartbeat_at or event.timestamp,
                )
                worker.heartbeat_at = worker.last_heartbeat_at
        elif event.event_type == "worker.superseded":
            worker = projection.workers.get(payload["worker_name"])
            if worker:
                worker.status = WorkerStatus.superseded
                worker.finished_at = event.timestamp
                worker.active_service_target = payload.get("active_service_target", "")
                worker.last_heartbeat_at = worker.last_heartbeat_at or event.timestamp
                worker.heartbeat_at = worker.last_heartbeat_at
        elif event.event_type == "archive.requested":
            archive = StageArchive.model_validate(payload["archive"])
            projection.archives[archive.id] = archive
            projection.run.status = RunStatus.awaiting_archive_approval
        elif event.event_type == "archive.restored":
            archive = projection.archives[payload["archive_id"]]
            archive.status = ArchiveStatus.restored
            archive.restored_at = event.timestamp
            stage = projection.stages.get(archive.stage_id)
            if stage:
                stage.status = StageStatus.rolled_back
                stage.updated_at = event.timestamp
            projection.run.status = RunStatus.rolled_back
        elif event.event_type == "merge.requested":
            batch_id = payload.get("batch_id", "")
            if batch_id:
                batch = projection.batches.get(batch_id)
                if batch:
                    batch.status = BatchStatus.pending_approval
                projection.run.status = RunStatus.executing
            else:
                projection.run.status = RunStatus.awaiting_merge_or_rollback
        elif event.event_type == "merge.completed":
            batch_id = payload.get("batch_id", "")
            if batch_id:
                projection.run.status = RunStatus.executing
            else:
                projection.run.status = RunStatus.completed
                stage = projection.stages.get(payload.get("stage_id", projection.run.current_stage_id))
                if stage:
                    stage.status = StageStatus.completed
                    stage.updated_at = event.timestamp
        elif event.event_type == "merge.blocked":
            projection.run.status = RunStatus.merge_blocked

        if projection is not None:
            projection.last_event_at = event.timestamp

    if projection is None:
        raise ValueError("No run projection available")

    projection.stats = ProjectionStats(
        event_count=len(events),
        constraint_count=len(projection.constraints),
        worker_count=len(projection.workers),
        archive_count=len(projection.archives),
        pending_recovery_count=sum(
            1
            for worker in projection.workers.values()
            if worker.status in {WorkerStatus.starting, WorkerStatus.stale}
        ),
        open_intervention_count=sum(
            1
            for intervention in projection.interventions.values()
            if intervention.status == InterventionStatus.open
        ),
        ready_feature_count=sum(
            1
            for feature in projection.features.values()
            if feature.status == FeatureStatus.ready
        ),
        open_batch_count=sum(
            1
            for batch in projection.batches.values()
            if batch.status in {
                BatchStatus.pending_approval,
                BatchStatus.integrating,
                BatchStatus.integration_failed,
                BatchStatus.pending_promote,
            }
        ),
    )
    return projection
