"""Domain models for BranchClaw."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def seconds_since(value: str) -> float | None:
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def new_id(prefix: str = "") -> str:
    value = uuid.uuid4().hex[:12]
    return f"{prefix}{value}" if prefix else value


def worktree_entry_id(
    *,
    worker_name: str,
    kind: str,
    stage_id: str,
    archive_id: str,
    workspace_path: str,
    head_sha: str,
    recorded_at: str,
) -> str:
    seed = "|".join(
        [
            worker_name,
            kind,
            stage_id,
            archive_id,
            workspace_path,
            head_sha,
            recorded_at,
        ]
    )
    return f"entry-{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:12]}"


class RunStatus(str, Enum):
    draft_plan = "draft_plan"
    awaiting_plan_approval = "awaiting_plan_approval"
    executing = "executing"
    awaiting_archive_approval = "awaiting_archive_approval"
    archived = "archived"
    awaiting_merge_or_rollback = "awaiting_merge_or_rollback"
    completed = "completed"
    rolled_back = "rolled_back"
    merge_blocked = "merge_blocked"


class StageStatus(str, Enum):
    draft = "draft"
    executing = "executing"
    archived = "archived"
    completed = "completed"
    rolled_back = "rolled_back"


class PlanStatus(str, Enum):
    pending_approval = "pending_approval"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"


class ApprovalType(str, Enum):
    plan = "plan"
    archive = "archive"
    rollback = "rollback"
    merge = "merge"
    promote = "promote"


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class WorkerStatus(str, Enum):
    starting = "starting"
    running = "running"
    stale = "stale"
    blocked = "blocked"
    stopped = "stopped"
    failed = "failed"
    superseded = "superseded"


class ProjectProfile(str, Enum):
    web = "web"
    fullstack = "fullstack"
    backend = "backend"


class WorkerResultStatus(str, Enum):
    success = "success"
    warning = "warning"
    blocked = "blocked"
    failed = "failed"


class ArchiveStatus(str, Enum):
    pending_approval = "pending_approval"
    approved = "approved"
    restored = "restored"


class InterventionStatus(str, Enum):
    open = "open"
    resolved = "resolved"


class DispatchMode(str, Enum):
    auto = "auto"
    manual = "manual"


class FeatureStatus(str, Enum):
    queued = "queued"
    assigned = "assigned"
    in_progress = "in_progress"
    ready = "ready"
    batched = "batched"
    merged = "merged"
    blocked = "blocked"
    dropped = "dropped"


class ValidationStatus(str, Enum):
    pending = "pending"
    passed = "passed"
    failed = "failed"


class BatchStatus(str, Enum):
    draft = "draft"
    pending_approval = "pending_approval"
    integrating = "integrating"
    integration_failed = "integration_failed"
    pending_promote = "pending_promote"
    completed = "completed"
    rejected = "rejected"


class RunRecord(BaseModel):
    id: str
    name: str
    description: str = ""
    project_profile: ProjectProfile = ProjectProfile.backend
    repo_root: str = ""
    base_ref: str = ""
    spec_content: str = ""
    rules_content: str = ""
    direction: str = ""
    integration_ref: str = ""
    max_active_features: int = 2
    dispatch_mode: DispatchMode = DispatchMode.auto
    default_backend: str = "tmux"
    default_command: list[str] = Field(default_factory=lambda: ["claude"])
    default_skip_permissions: bool = False
    created_at: str = Field(default_factory=now_iso)
    status: RunStatus = RunStatus.draft_plan
    current_stage_id: str = ""
    active_plan_id: str = ""
    needs_replan: bool = False
    dirty_reason: str = ""
    dirty_since: str = ""
    dirty_stage_id: str = ""
    latest_constraint_id: str = ""
    latest_constraint_at: str = ""


class StageRecord(BaseModel):
    id: str
    run_id: str
    name: str
    status: StageStatus = StageStatus.draft
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class PlanProposal(BaseModel):
    id: str
    run_id: str
    stage_id: str
    author: str = ""
    summary: str = ""
    content: str
    effective_bundle: str = ""
    status: PlanStatus = PlanStatus.pending_approval
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    feedback: str = ""


class ApprovalGate(BaseModel):
    id: str
    run_id: str
    stage_id: str = ""
    gate_type: ApprovalType
    target_id: str
    status: ApprovalStatus = ApprovalStatus.pending
    created_at: str = Field(default_factory=now_iso)
    resolved_at: str = ""
    actor: str = ""
    feedback: str = ""


class ConstraintPatch(BaseModel):
    id: str
    run_id: str
    author: str = ""
    content: str
    created_at: str = Field(default_factory=now_iso)


class FeatureRecord(BaseModel):
    id: str
    run_id: str
    stage_id: str
    title: str
    goal: str = ""
    task: str = ""
    status: FeatureStatus = FeatureStatus.queued
    claimed_areas: list[str] = Field(default_factory=list)
    claimed_files: list[str] = Field(default_factory=list)
    priority: int = 100
    worker_name: str = ""
    archive_id: str = ""
    result_summary: str = ""
    validation_status: ValidationStatus = ValidationStatus.pending
    validation_command: str = ""
    validation_output: str = ""
    validation_ran_at: str = ""
    snapshot_branch: str = ""
    snapshot_head_sha: str = ""
    snapshot_workspace_path: str = ""
    snapshot_recorded_at: str = ""
    result: WorkerResult | None = None
    integration_blocker: str = ""
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class BatchRecord(BaseModel):
    id: str
    run_id: str
    stage_id: str
    feature_ids: list[str] = Field(default_factory=list)
    status: BatchStatus = BatchStatus.draft
    integration_ref: str = ""
    validation_command: str = ""
    validation_output: str = ""
    validation_status: ValidationStatus = ValidationStatus.pending
    created_at: str = Field(default_factory=now_iso)
    approved_at: str = ""
    promoted_at: str = ""


class WorkerResult(BaseModel):
    status: WorkerResultStatus = WorkerResultStatus.success
    project_profile: ProjectProfile = ProjectProfile.backend
    stack: str = ""
    runtime: str = ""
    package_manager: str = ""
    install_command: str = ""
    start_command: str = ""
    preview_url: str = ""
    backend_url: str = ""
    output_snippet: str = ""
    changed_surface_summary: str = ""
    architecture_summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    reported_at: str = Field(default_factory=now_iso)


class WorkerMcpSession(BaseModel):
    token_id: str
    token: str
    run_id: str
    worker_name: str
    stage_id: str
    workspace_path: str
    repo_root: str
    project_profile: ProjectProfile = ProjectProfile.backend
    task: str = ""
    server_url: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    revoked_at: str = ""


class WorkerRuntime(BaseModel):
    worker_name: str
    run_id: str
    stage_id: str
    feature_id: str = ""
    workspace_path: str
    branch: str
    base_ref: str
    head_sha: str = ""
    backend: str = "subprocess"
    pid: int = 0
    child_pid: int = 0
    supervisor_pid: int = 0
    tmux_target: str = ""
    task: str = ""
    heartbeat_at: str = Field(default_factory=now_iso)
    last_heartbeat_at: str = Field(default_factory=now_iso)
    started_at: str = Field(default_factory=now_iso)
    finished_at: str = ""
    stale_at: str = ""
    blocked_at: str = ""
    blocked_reason: str = ""
    exit_code: int | None = None
    failure_reason: str = ""
    status: WorkerStatus = WorkerStatus.starting
    result: WorkerResult | None = None
    mcp_enabled: bool = False
    mcp_server_url: str = ""
    mcp_token_id: str = ""
    last_tool_name: str = ""
    last_tool_status: str = ""
    last_tool_at: str = ""
    last_tool_error: str = ""
    last_tool_arguments: dict[str, Any] = Field(default_factory=dict)
    active_service_target: str = ""
    active_service_log_path: str = ""
    discovered_url: str = ""
    report_source: str = ""
    tool_retry_count: int = 0
    last_failed_diff_signature: str = ""
    remediation_attempt_count: int = 0
    restart_attempt_count: int = 0
    last_remediation_action: str = ""
    last_remediation_status: str = ""
    last_remediation_at: str = ""
    intervention_id: str = ""
    managed_by_daemon: bool = False
    daemon_pid: int = 0


class WorkerIntervention(BaseModel):
    id: str
    run_id: str
    worker_name: str
    feature_id: str = ""
    status: InterventionStatus = InterventionStatus.open
    reason: str = ""
    recommended_action: str = ""
    last_tool_name: str = ""
    last_tool_error: str = ""
    remediation_attempts: int = 0
    restart_attempts: int = 0
    related_entry_id: str = ""
    created_at: str = Field(default_factory=now_iso)
    resolved_at: str = ""
    resolution_reason: str = ""


class ArchiveWorkspace(BaseModel):
    worker_name: str
    stage_id: str
    feature_id: str = ""
    workspace_path: str
    branch: str
    base_ref: str
    head_sha: str
    backend: str = ""
    task: str = ""
    result: WorkerResult | None = None


class StageArchive(BaseModel):
    id: str
    run_id: str
    stage_id: str
    label: str = ""
    summary: str = ""
    status: ArchiveStatus = ArchiveStatus.pending_approval
    created_at: str = Field(default_factory=now_iso)
    restored_at: str = ""
    state_snapshot: dict[str, Any] = Field(default_factory=dict)
    workspaces: list[ArchiveWorkspace] = Field(default_factory=list)


class ManagedProcessRecord(BaseModel):
    id: str
    data_dir: str
    process_kind: str
    process_key: str
    pid: int = 0
    child_pid: int = 0
    supervisor_pid: int = 0
    run_id: str = ""
    worker_name: str = ""
    host: str = ""
    port: int = 0
    socket: str = ""
    status: str = "running"
    started_at: str = Field(default_factory=now_iso)
    last_seen_at: str = Field(default_factory=now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DataDirRegistry(BaseModel):
    data_dir: str
    process_ids: list[str] = Field(default_factory=list)
    last_seen_at: str = Field(default_factory=now_iso)


class DaemonStatus(BaseModel):
    running: bool = False
    daemon_pid: int = 0
    socket_path: str = ""
    started_at: str = ""
    dashboard_running: bool = False
    dashboard_host: str = ""
    dashboard_port: int = 0
    dashboard_url: str = ""
    data_dirs: list[DataDirRegistry] = Field(default_factory=list)
    processes: list[ManagedProcessRecord] = Field(default_factory=list)


class EventRecord(BaseModel):
    id: str
    run_id: str
    sequence: int
    event_type: str
    timestamp: str = Field(default_factory=now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)


class ProjectionStats(BaseModel):
    event_count: int = 0
    constraint_count: int = 0
    worker_count: int = 0
    archive_count: int = 0
    pending_recovery_count: int = 0
    open_intervention_count: int = 0
    ready_feature_count: int = 0
    open_batch_count: int = 0


class RunProjection(BaseModel):
    run: RunRecord
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    plans: dict[str, PlanProposal] = Field(default_factory=dict)
    approvals: dict[str, ApprovalGate] = Field(default_factory=dict)
    constraints: list[ConstraintPatch] = Field(default_factory=list)
    features: dict[str, FeatureRecord] = Field(default_factory=dict)
    batches: dict[str, BatchRecord] = Field(default_factory=dict)
    workers: dict[str, WorkerRuntime] = Field(default_factory=dict)
    interventions: dict[str, WorkerIntervention] = Field(default_factory=dict)
    archives: dict[str, StageArchive] = Field(default_factory=dict)
    stats: ProjectionStats = Field(default_factory=ProjectionStats)
    last_event_at: str = ""
