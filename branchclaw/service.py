"""Core BranchClaw application service."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from branchclaw.config import get_data_dir, load_config
from branchclaw.daemon import (
    BranchClawDaemonClient,
    BranchClawDaemonError,
    in_daemon_process,
)
from branchclaw.mcp_state import (
    command_supports_mcp,
    create_worker_mcp_session,
    revoke_worker_mcp_session,
)
from branchclaw.models import (
    ApprovalGate,
    ApprovalStatus,
    ApprovalType,
    ArchiveStatus,
    BatchRecord,
    BatchStatus,
    ConstraintPatch,
    DispatchMode,
    FeatureRecord,
    FeatureStatus,
    InterventionStatus,
    PlanProposal,
    PlanStatus,
    ProjectProfile,
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
    new_id,
    now_iso,
    seconds_since,
    worktree_entry_id,
)
from branchclaw.project_profiles import normalize_project_profile, render_project_skill_prompt
from branchclaw.project_tools import detect_project_stack, install_dependencies, wait_for_url
from branchclaw.runtime import (
    clear_worker_runtime_state,
    pid_alive,
    read_worker_status,
    terminate_tmux_target,
    tmux_target_alive,
    tmux_target_pid,
    worker_launch_path,
    worker_stop_path,
    write_worker_status,
)
from branchclaw.storage import EventStore, artifacts_dir, save_json
from branchclaw.workspace import GitWorkspaceRuntimeAdapter


class BranchClawError(RuntimeError):
    """Raised for invalid BranchClaw operations."""


LIVE_WORKER_STATUSES = {
    WorkerStatus.starting,
    WorkerStatus.running,
    WorkerStatus.stale,
}
TERMINAL_WORKER_STATUSES = {
    WorkerStatus.blocked,
    WorkerStatus.stopped,
    WorkerStatus.failed,
    WorkerStatus.superseded,
}
BLOCKING_WORKER_STATUSES = LIVE_WORKER_STATUSES | {WorkerStatus.blocked}
FINAL_RUN_STATUSES = {
    RunStatus.archived,
    RunStatus.completed,
    RunStatus.rolled_back,
}


def _selected_workers(
    projection: RunProjection,
    worker_names: list[str] | None = None,
) -> list[WorkerRuntime]:
    selected = set(worker_names or projection.workers.keys())
    return [
        worker
        for worker_name, worker in projection.workers.items()
        if worker_name in selected
    ]


class BranchClawService:
    """High-level service that mutates BranchClaw state via events."""

    def __init__(self, store: EventStore | None = None):
        self.store = store or EventStore()

    def create_run(
        self,
        name: str,
        *,
        description: str = "",
        project_profile: str | ProjectProfile = ProjectProfile.backend,
        spec_content: str = "",
        rules_content: str = "",
        repo: str | None = None,
        direction: str = "",
        integration_ref: str = "",
        max_active_features: int = 2,
        dispatch_mode: str | DispatchMode = DispatchMode.auto,
        default_backend: str = "",
        default_command: list[str] | None = None,
        default_skip_permissions: bool | None = None,
    ) -> RunProjection:
        adapter = GitWorkspaceRuntimeAdapter(repo)
        config = load_config()
        normalized_profile = normalize_project_profile(project_profile)
        run_id = f"{_slug(name)}-{uuid.uuid4().hex[:6]}"
        stage_id = "stage-1"
        normalized_dispatch = (
            dispatch_mode
            if isinstance(dispatch_mode, DispatchMode)
            else DispatchMode(str(dispatch_mode or DispatchMode.auto.value))
        )
        resolved_default_command = [str(item) for item in (default_command or []) if str(item).strip()]
        if not resolved_default_command:
            resolved_default_command = shlex.split(config.default_agent_command.strip()) or ["claude"]
        resolved_integration_ref = (
            integration_ref.strip()
            or f"branchclaw/{_slug(run_id)}/integration"
        )
        run = RunRecord(
            id=run_id,
            name=name,
            description=description,
            project_profile=normalized_profile,
            repo_root=adapter.repo_root(),
            base_ref=adapter.default_base_ref(),
            spec_content=spec_content,
            rules_content=rules_content,
            direction=direction or description or spec_content[:240],
            integration_ref=resolved_integration_ref,
            max_active_features=max(1, int(max_active_features)),
            dispatch_mode=normalized_dispatch,
            default_backend=(default_backend or config.default_backend or "tmux").strip() or "tmux",
            default_command=resolved_default_command,
            default_skip_permissions=(
                config.skip_permissions
                if default_skip_permissions is None
                else bool(default_skip_permissions)
            ),
            current_stage_id=stage_id,
        )
        stage = StageRecord(
            id=stage_id,
            run_id=run_id,
            name="Initial Stage",
            status=StageStatus.draft,
        )

        art_dir = artifacts_dir(run_id)
        (art_dir / "spec.md").write_text(spec_content, encoding="utf-8")
        (art_dir / "rules.md").write_text(rules_content, encoding="utf-8")

        self.store.append(run_id, "run.created", {"run": json.loads(run.model_dump_json())})
        self.store.append(run_id, "stage.created", {"stage": json.loads(stage.model_dump_json())})
        return self.get_run(run_id)

    def list_runs(self) -> list[RunProjection]:
        return self.store.list_runs()

    def get_run(self, run_id: str, *, rebuild: bool = False) -> RunProjection:
        return self.store.load_projection(run_id, rebuild=rebuild)

    def compile_execution_bundle(self, projection: RunProjection, *, stage_context: str = "") -> str:
        constraints = "\n".join(f"- {item.content}" for item in projection.constraints) or "- None"
        stage = projection.stages.get(projection.run.current_stage_id)
        stage_lines = [
            f"- Stage ID: {projection.run.current_stage_id}",
            f"- Stage Status: {stage.status.value if stage else 'unknown'}",
            f"- Active Plan: {projection.run.active_plan_id or '(none)'}",
            f"- Needs Replan: {'yes' if projection.run.needs_replan else 'no'}",
            f"- Project Profile: {projection.run.project_profile.value}",
        ]
        if projection.run.needs_replan:
            stage_lines.extend(
                [
                    f"- Dirty Reason: {projection.run.dirty_reason or 'constraint'}",
                    f"- Dirty Since: {projection.run.dirty_since or '(unknown)'}",
                    f"- Dirty Stage: {projection.run.dirty_stage_id or projection.run.current_stage_id}",
                    f"- Latest Constraint ID: {projection.run.latest_constraint_id or '(none)'}",
                ]
            )
        if stage_context:
            stage_lines.append(f"- Planner Context: {stage_context}")

        return "\n".join(
            [
                "# Base Spec",
                projection.run.spec_content or "(empty)",
                "",
                "# Shared Rules",
                projection.run.rules_content or "(empty)",
                "",
                "# Approved Constraints",
                constraints,
                "",
                "# Planner State",
                *stage_lines,
            ]
        )

    def propose_plan(
        self,
        run_id: str,
        content: str,
        *,
        summary: str = "",
        author: str = "",
    ) -> tuple[PlanProposal, ApprovalGate]:
        projection = self.get_run(run_id)
        if projection.run.active_plan_id:
            current = projection.plans.get(projection.run.active_plan_id)
            if current and current.status in {PlanStatus.pending_approval, PlanStatus.approved}:
                self.store.append(run_id, "plan.superseded", {"plan_id": current.id})

        stage_id = projection.run.current_stage_id
        plan = PlanProposal(
            id=new_id("plan-"),
            run_id=run_id,
            stage_id=stage_id,
            author=author,
            summary=summary,
            content=content,
            effective_bundle=self.compile_execution_bundle(
                projection,
                stage_context=summary or content[:120],
            ),
        )
        gate = ApprovalGate(
            id=new_id("gate-"),
            run_id=run_id,
            stage_id=stage_id,
            gate_type=ApprovalType.plan,
            target_id=plan.id,
        )
        self.store.append(run_id, "plan.proposed", {"plan": json.loads(plan.model_dump_json())})
        self.store.append(run_id, "approval.requested", {"gate": json.loads(gate.model_dump_json())})
        self._write_plan_artifact(run_id, plan)
        return plan, gate

    def resume_planner(self, run_id: str, *, actor: str = "", note: str = "") -> str:
        projection = self.get_run(run_id, rebuild=True)
        dirty_context = self._planner_dirty_context(projection)
        bundle = self.compile_execution_bundle(
            projection,
            stage_context="\n".join(part for part in [dirty_context, note] if part),
        )
        self.store.append(
            run_id,
            "planner.resumed",
            {
                "actor": actor,
                "note": note,
                "status": projection.run.status.value,
                "needs_replan": projection.run.needs_replan,
            },
        )
        return bundle

    def approve_gate(
        self,
        run_id: str,
        gate_id: str,
        *,
        actor: str = "",
        feedback: str = "",
    ) -> RunProjection:
        projection = self.get_run(run_id, rebuild=True)
        gate = projection.approvals.get(gate_id)
        if gate is None:
            raise BranchClawError(f"Approval gate '{gate_id}' not found")
        if gate.status != ApprovalStatus.pending:
            raise BranchClawError(f"Approval gate '{gate_id}' is already {gate.status.value}")

        if gate.gate_type in {ApprovalType.archive, ApprovalType.merge, ApprovalType.rollback}:
            projection = self.reconcile_workers(run_id)
            gate = projection.approvals.get(gate_id)
            if gate is None:
                raise BranchClawError(f"Approval gate '{gate_id}' not found after reconcile")

        if gate.gate_type in {ApprovalType.archive, ApprovalType.merge} and projection.run.needs_replan:
            raise BranchClawError(
                f"Run '{run_id}' requires replan before approving {gate.gate_type.value}"
            )

        if gate.gate_type == ApprovalType.archive:
            self._ensure_workers_safe_for_archive(projection)

        self.store.append(
            run_id,
            "approval.approved",
            {"gate_id": gate_id, "actor": actor, "feedback": feedback},
        )

        if gate.gate_type == ApprovalType.plan and projection.run.needs_replan:
            self.store.append(
                run_id,
                "plan.replan_cleared",
                {"plan_id": gate.target_id, "actor": actor},
            )
        if gate.gate_type == ApprovalType.plan:
            approved_projection = self.get_run(run_id, rebuild=True)
            approved_plan = approved_projection.plans.get(gate.target_id)
            if approved_plan is not None:
                self._sync_features_from_plan(run_id, approved_plan)

        if gate.gate_type == ApprovalType.merge:
            self._execute_merge(run_id, gate.target_id)
        elif gate.gate_type == ApprovalType.promote:
            self._execute_promote(run_id, gate.target_id)
        elif gate.gate_type == ApprovalType.rollback:
            self._execute_restore(run_id, gate.target_id)

        updated = self.get_run(run_id, rebuild=True)
        if updated.run.status in FINAL_RUN_STATUSES:
            self._resolve_worker_interventions(run_id, resolution_reason=f"run_{updated.run.status.value}")
            updated = self.get_run(run_id, rebuild=True)
        return updated

    def reject_gate(
        self,
        run_id: str,
        gate_id: str,
        *,
        actor: str = "",
        feedback: str = "",
    ) -> RunProjection:
        projection = self.get_run(run_id)
        gate = projection.approvals.get(gate_id)
        if gate is None:
            raise BranchClawError(f"Approval gate '{gate_id}' not found")
        if gate.status != ApprovalStatus.pending:
            raise BranchClawError(f"Approval gate '{gate_id}' is already {gate.status.value}")

        self.store.append(
            run_id,
            "approval.rejected",
            {"gate_id": gate_id, "actor": actor, "feedback": feedback},
        )
        return self.get_run(run_id, rebuild=True)

    def add_constraint(self, run_id: str, content: str, *, author: str = "") -> ConstraintPatch:
        projection = self.get_run(run_id)
        constraint = ConstraintPatch(
            id=new_id("constraint-"),
            run_id=run_id,
            author=author,
            content=content,
        )
        self.store.append(
            run_id,
            "constraint.added",
            {"constraint": json.loads(constraint.model_dump_json())},
        )
        self.store.append(
            run_id,
            "plan.replan_requested",
            {
                "reason": "constraint",
                "stage_id": projection.run.current_stage_id,
                "constraint_id": constraint.id,
                "constraint_at": constraint.created_at,
                "author": author,
            },
        )
        return constraint

    def list_constraints(self, run_id: str) -> list[ConstraintPatch]:
        return self.get_run(run_id).constraints

    def list_features(self, run_id: str) -> list[FeatureRecord]:
        projection = self.get_run(run_id, rebuild=True)
        return sorted(
            projection.features.values(),
            key=lambda item: (item.status.value, item.priority, item.created_at),
        )

    def get_feature(self, run_id: str, feature_id: str) -> FeatureRecord:
        projection = self.get_run(run_id, rebuild=True)
        feature = projection.features.get(feature_id)
        if feature is None:
            raise BranchClawError(f"Feature '{feature_id}' not found")
        return feature

    def list_batches(self, run_id: str) -> list[BatchRecord]:
        projection = self.get_run(run_id, rebuild=True)
        return sorted(projection.batches.values(), key=lambda item: item.created_at, reverse=True)

    def get_batch(self, run_id: str, batch_id: str) -> BatchRecord:
        projection = self.get_run(run_id, rebuild=True)
        batch = projection.batches.get(batch_id)
        if batch is None:
            raise BranchClawError(f"Batch '{batch_id}' not found")
        return batch

    def dispatch_feature_backlog(self, run_id: str) -> RunProjection:
        projection = self.get_run(run_id, rebuild=True)
        projection = self._refresh_feature_state(projection)
        projection = self.get_run(run_id, rebuild=True)
        self._ensure_ready_batch(projection)
        projection = self.get_run(run_id, rebuild=True)
        if projection.run.status != RunStatus.executing:
            return projection
        if projection.run.dispatch_mode != DispatchMode.auto:
            return projection
        if projection.run.needs_replan:
            return projection

        active = [
            feature
            for feature in projection.features.values()
            if feature.status in {FeatureStatus.assigned, FeatureStatus.in_progress}
        ]
        available_slots = max(0, projection.run.max_active_features - len(active))
        if available_slots <= 0:
            return projection

        claimed_areas = {area for feature in active for area in feature.claimed_areas}
        claimed_files = {path for feature in active for path in feature.claimed_files}
        queued = sorted(
            (
                feature
                for feature in projection.features.values()
                if feature.status == FeatureStatus.queued
            ),
            key=lambda item: (item.priority, item.created_at),
        )
        for feature in queued:
            if available_slots <= 0:
                break
            if self._feature_conflicts(feature, claimed_areas, claimed_files):
                continue
            worker_name = self._feature_worker_name(feature)
            try:
                self.spawn_worker(
                    run_id,
                    worker_name,
                    command=projection.run.default_command,
                    backend=projection.run.default_backend,
                    task=feature.task or feature.goal or feature.title,
                    feature_id=feature.id,
                    skip_permissions=projection.run.default_skip_permissions,
                )
            except BranchClawError:
                blocked = feature.model_copy(
                    update={
                        "status": FeatureStatus.blocked,
                        "integration_blocker": "automatic dispatch failed",
                        "updated_at": now_iso(),
                    }
                )
                self.store.append(
                    run_id,
                    "feature.blocked",
                    {"feature": json.loads(blocked.model_dump_json())},
                )
                continue
            self.store.append(
                run_id,
                "feature.assigned",
                {"feature_id": feature.id, "worker_name": worker_name},
            )
            claimed_areas.update(feature.claimed_areas)
            claimed_files.update(feature.claimed_files)
            available_slots -= 1
        return self.get_run(run_id, rebuild=True)

    def spawn_worker(
        self,
        run_id: str,
        worker_name: str,
        *,
        command: list[str],
        backend: str = "subprocess",
        task: str = "",
        feature_id: str = "",
        skip_permissions: bool | None = None,
        remediation_attempt_count: int = 0,
        restart_attempt_count: int = 0,
    ) -> WorkerRuntime:
        projection = self.get_run(run_id, rebuild=True)
        if projection.run.status not in {
            RunStatus.executing,
            RunStatus.awaiting_plan_approval,
            RunStatus.awaiting_archive_approval,
        }:
            raise BranchClawError(
                f"Run '{run_id}' is in status '{projection.run.status.value}', not ready for workers"
            )

        existing = projection.workers.get(worker_name)
        if existing and existing.status not in TERMINAL_WORKER_STATUSES:
            raise BranchClawError(f"Worker '{worker_name}' is already {existing.status.value}")

        adapter = GitWorkspaceRuntimeAdapter(projection.run.repo_root)
        snapshot = adapter.create_workspace(
            run_id=run_id,
            stage_id=projection.run.current_stage_id,
            worker_name=worker_name,
        )
        mcp_enabled = command_supports_mcp(command, backend=backend)
        mcp_server_url = ""
        mcp_session = None
        if mcp_enabled:
            try:
                daemon = BranchClawDaemonClient.require_running()
                server_status = daemon.ensure_mcp_server(
                    data_dir=str(get_data_dir().resolve()),
                    run_id=run_id,
                )
            except BranchClawDaemonError as exc:
                raise BranchClawError(str(exc)) from exc
            mcp_server_url = str(server_status.get("base_url", ""))
            self.store.append(
                run_id,
                "mcp.server_started",
                {
                    "base_url": mcp_server_url,
                    "pid": int(server_status.get("pid", 0)),
                    "reused": bool(server_status.get("reused", False)),
                },
            )
            mcp_session = create_worker_mcp_session(
                run_id=run_id,
                worker_name=worker_name,
                stage_id=projection.run.current_stage_id,
                workspace_path=snapshot.workspace_path,
                repo_root=projection.run.repo_root,
                project_profile=projection.run.project_profile,
                task=task,
                server_url=mcp_server_url,
            )
        env = {
            "BRANCHCLAW_RUN_ID": run_id,
            "BRANCHCLAW_WORKER_NAME": worker_name,
            "BRANCHCLAW_STAGE_ID": projection.run.current_stage_id,
            "BRANCHCLAW_PROJECT_PROFILE": projection.run.project_profile.value,
        }
        effective_skip_permissions = (
            load_config().skip_permissions if skip_permissions is None else skip_permissions
        )
        prompt = self._build_worker_prompt(
            projection,
            worker_name,
            task,
            mcp_enabled=mcp_enabled,
        )
        system_prompt = self._build_worker_system_prompt(
            projection,
            worker_name,
            task,
            mcp_enabled=mcp_enabled,
        )
        clear_worker_runtime_state(run_id, worker_name)
        save_json(
            worker_launch_path(run_id, worker_name),
            {
                "run_id": run_id,
                "worker_name": worker_name,
                "stage_id": projection.run.current_stage_id,
                "repo_root": projection.run.repo_root,
                "workspace_path": snapshot.workspace_path,
                "branch": snapshot.branch,
                "base_ref": snapshot.base_ref,
                "backend": backend,
                "task": task,
                "feature_id": feature_id,
                "command": command,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "skip_permissions": effective_skip_permissions,
                "env": env,
                "heartbeat_interval": load_config().heartbeat_interval,
                "claude_ready_timeout": load_config().claude_ready_timeout,
                "mcp_enabled": mcp_enabled,
                "mcp_server_url": mcp_server_url,
                "mcp_token_id": mcp_session.token_id if mcp_session else "",
                "mcp_token": mcp_session.token if mcp_session else "",
                "remediation_attempt_count": remediation_attempt_count,
                "restart_attempt_count": restart_attempt_count,
                "managed_by_daemon": True,
                "daemon_pid": 0,
            },
        )
        try:
            daemon = BranchClawDaemonClient.require_running()
            supervisor = daemon.launch_supervisor(
                data_dir=str(get_data_dir().resolve()),
                run_id=run_id,
                worker_name=worker_name,
            )
        except BranchClawDaemonError as exc:
            if mcp_session:
                revoke_worker_mcp_session(
                    mcp_session.token_id,
                    run_id=run_id,
                    reason="spawn_failed",
                )
            raise BranchClawError(str(exc)) from exc
        write_worker_status(
            run_id,
            worker_name,
            {
                "status": "starting",
                "backend": backend,
                "supervisor_pid": int(supervisor.get("supervisorPid", 0)),
                "child_pid": 0,
                "tmux_target": "",
                "started_at": now_iso(),
                "workspace_path": snapshot.workspace_path,
                "managed_by_daemon": True,
                "daemon_pid": int(supervisor.get("daemonPid", 0)),
            },
        )
        return self._await_worker_start(
            run_id,
            worker_name,
            int(supervisor.get("supervisorPid", 0)),
        )

    def report_worker_result(
        self,
        run_id: str,
        worker_name: str,
        result_payload: dict[str, Any],
        *,
        source: str = "operator",
    ) -> WorkerRuntime:
        projection = self.get_run(run_id, rebuild=True)
        worker = projection.workers.get(worker_name)
        if worker is None:
            raise BranchClawError(f"Worker '{worker_name}' not found")

        payload = dict(result_payload)
        payload.setdefault("project_profile", projection.run.project_profile.value)
        result = WorkerResult.model_validate(payload)
        self.store.append(
            run_id,
            "worker.reported",
            {
                "worker_name": worker_name,
                "result": json.loads(result.model_dump_json()),
                "source": source,
            },
        )
        if result.preview_url or result.backend_url:
            self.store.append(
                run_id,
                "worker.preview_updated",
                {
                    "worker_name": worker_name,
                    "preview_url": result.preview_url,
                    "backend_url": result.backend_url,
                    "start_command": result.start_command,
                    "reported_at": result.reported_at,
                },
            )
        self._write_worker_result_artifact(run_id, worker_name, result)
        self._resolve_worker_interventions(
            run_id,
            worker_name=worker_name,
            resolution_reason="result_reported",
        )
        return self.get_run(run_id, rebuild=True).workers[worker_name]

    def checkpoint_worker(self, run_id: str, worker_name: str, *, message: str = "") -> WorkerRuntime:
        projection = self.get_run(run_id)
        worker = projection.workers.get(worker_name)
        if worker is None:
            raise BranchClawError(f"Worker '{worker_name}' not found")
        adapter = GitWorkspaceRuntimeAdapter(projection.run.repo_root)
        msg = message or f"[branchclaw] checkpoint {worker_name} @ {now_iso()}"
        adapter.checkpoint(worker.workspace_path, msg)
        self.store.append(
            run_id,
            "worker.checkpoint",
            {
                "worker_name": worker_name,
                "head_sha": adapter.head_sha(worker.workspace_path),
                "message": msg,
            },
        )
        return self.get_run(run_id, rebuild=True).workers[worker_name]

    def restart_worker(
        self,
        run_id: str,
        worker_name: str,
        *,
        auto: bool = False,
    ) -> WorkerRuntime:
        launch_path = worker_launch_path(run_id, worker_name)
        if not launch_path.exists():
            raise BranchClawError(f"Missing worker launch payload for '{worker_name}'")
        try:
            payload = json.loads(launch_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise BranchClawError(f"Invalid worker launch payload for '{worker_name}'") from exc

        command = [str(item) for item in payload.get("command", []) if str(item)]
        if not command:
            raise BranchClawError(f"Missing worker command for '{worker_name}'")
        projection = self.get_run(run_id, rebuild=True)
        existing = projection.workers.get(worker_name)
        remediation_attempt_count = 0
        restart_attempt_count = 0
        if auto and existing is not None:
            remediation_attempt_count = existing.remediation_attempt_count
            restart_attempt_count = existing.restart_attempt_count + 1
        worker = self.spawn_worker(
            run_id,
            worker_name,
            command=command,
            backend=str(payload.get("backend") or "subprocess"),
            task=str(payload.get("task") or ""),
            feature_id=str(payload.get("feature_id") or ""),
            skip_permissions=payload.get("skip_permissions"),
            remediation_attempt_count=remediation_attempt_count,
            restart_attempt_count=restart_attempt_count,
        )
        if not auto:
            self._resolve_worker_interventions(
                run_id,
                worker_name=worker_name,
                resolution_reason="manual_restart",
            )
            worker = self.get_run(run_id, rebuild=True).workers[worker_name]
        return worker

    def stop_worker(self, run_id: str, worker_name: str) -> WorkerRuntime:
        projection = self.get_run(run_id, rebuild=True)
        worker = projection.workers.get(worker_name)
        if worker is None:
            raise BranchClawError(f"Worker '{worker_name}' not found")

        try:
            daemon = BranchClawDaemonClient.require_running()
            daemon.stop_worker(
                data_dir=str(get_data_dir().resolve()),
                run_id=run_id,
                worker_name=worker_name,
            )
        except BranchClawDaemonError as exc:
            raise BranchClawError(str(exc)) from exc
        deadline = time.time() + 4.0
        while time.time() < deadline:
            current = self.get_run(run_id, rebuild=True).workers.get(worker_name)
            if current and current.status in TERMINAL_WORKER_STATUSES:
                return current
            time.sleep(0.1)

        current = self.get_run(run_id, rebuild=True).workers.get(worker_name)
        if current is not None:
            self._force_worker_termination(current)
        reconciled = self.reconcile_workers(run_id, worker_names=[worker_name])
        current = reconciled.workers.get(worker_name)
        if current is None:
            raise BranchClawError(f"Worker '{worker_name}' not found after reconcile")
        return current

    def reconcile_workers(
        self,
        run_id: str,
        *,
        worker_names: list[str] | None = None,
    ) -> RunProjection:
        projection = self.get_run(run_id, rebuild=True)
        selected_workers = _selected_workers(projection, worker_names)
        requires_process_observation = any(
            worker.status not in TERMINAL_WORKER_STATUSES
            for worker in selected_workers
            if worker.status != WorkerStatus.superseded
        )
        if in_daemon_process() or not requires_process_observation:
            self._reconcile_workers_local(run_id, worker_names=worker_names)
            return self._apply_worker_watchdog_policies_local(run_id, worker_names=worker_names)
        try:
            daemon = BranchClawDaemonClient.require_running()
            daemon.reconcile_run(
                data_dir=str(get_data_dir().resolve()),
                run_id=run_id,
                worker_names=worker_names,
            )
        except BranchClawDaemonError as exc:
            raise BranchClawError(str(exc)) from exc
        return self.get_run(run_id, rebuild=True)

    def _reconcile_workers_local(
        self,
        run_id: str,
        *,
        worker_names: list[str] | None = None,
    ) -> RunProjection:
        projection = self.get_run(run_id, rebuild=True)
        adapter = GitWorkspaceRuntimeAdapter(projection.run.repo_root)
        stale_after = max(0.5, load_config().stale_after)
        selected = set(worker_names or projection.workers.keys())

        for worker_name in selected:
            worker = projection.workers.get(worker_name)
            if worker is None:
                continue
            if worker.status == WorkerStatus.superseded:
                continue

            if projection.run.status in FINAL_RUN_STATUSES and worker.status in LIVE_WORKER_STATUSES:
                self._request_worker_shutdown(worker)

            status = read_worker_status(run_id, worker_name)
            worker = self.get_run(run_id, rebuild=True).workers.get(worker_name, worker)
            observed = self._observe_worker_state(worker, status)
            heartbeat_age = seconds_since(worker.last_heartbeat_at or worker.heartbeat_at) or 0.0

            if observed["alive"] and heartbeat_age > stale_after:
                if worker.status != WorkerStatus.stale:
                    self.store.append(
                        run_id,
                        "worker.stale",
                        {
                            "worker_name": worker_name,
                            "stale_at": now_iso(),
                            "last_heartbeat_at": worker.last_heartbeat_at or worker.heartbeat_at,
                        },
                    )
                self.store.append(
                    run_id,
                    "worker.reconciled",
                    {
                        "worker_name": worker_name,
                        "status": WorkerStatus.running.value,
                        "head_sha": self._safe_head_sha(adapter, worker.workspace_path),
                        "child_pid": observed["child_pid"],
                        "supervisor_pid": observed["supervisor_pid"],
                        "tmux_target": observed["tmux_target"],
                        "last_heartbeat_at": now_iso(),
                        "stale_at": "",
                    },
                )
            elif observed["alive"] and worker.status in {WorkerStatus.starting, WorkerStatus.stale}:
                self.store.append(
                    run_id,
                    "worker.reconciled",
                    {
                        "worker_name": worker_name,
                        "status": WorkerStatus.running.value,
                        "head_sha": self._safe_head_sha(adapter, worker.workspace_path),
                        "child_pid": observed["child_pid"],
                        "supervisor_pid": observed["supervisor_pid"],
                        "tmux_target": observed["tmux_target"],
                        "last_heartbeat_at": now_iso(),
                        "stale_at": "",
                    },
                )
            elif not observed["alive"] and worker.status in LIVE_WORKER_STATUSES:
                if worker.status != WorkerStatus.stale and heartbeat_age > stale_after:
                    self.store.append(
                        run_id,
                        "worker.stale",
                        {
                            "worker_name": worker_name,
                            "stale_at": now_iso(),
                            "last_heartbeat_at": worker.last_heartbeat_at or worker.heartbeat_at,
                        },
                    )
                self.store.append(
                    run_id,
                    "worker.exited",
                    {
                        "worker_name": worker_name,
                        "child_pid": 0,
                        "supervisor_pid": observed["supervisor_pid"],
                        "tmux_target": observed["tmux_target"],
                        "exit_code": observed["exit_code"],
                        "finished_at": now_iso(),
                        "explicit_stop": observed["explicit_stop"],
                    },
                )
                terminal_event = "worker.stopped" if observed["explicit_stop"] or observed["exit_code"] == 0 else "worker.failed"
                self.store.append(
                    run_id,
                    terminal_event,
                    {
                        "worker_name": worker_name,
                        "child_pid": 0,
                        "supervisor_pid": observed["supervisor_pid"],
                        "tmux_target": observed["tmux_target"],
                        "exit_code": observed["exit_code"],
                        "finished_at": now_iso(),
                        "explicit_stop": observed["explicit_stop"],
                        "failure_reason": observed["failure_reason"],
                    },
                )
                if worker.mcp_token_id:
                    revoke_worker_mcp_session(
                        worker.mcp_token_id,
                        run_id=run_id,
                        reason="reconcile_terminal",
                    )
            elif not observed["alive"] and worker.status == WorkerStatus.blocked:
                self.store.append(
                    run_id,
                    "worker.exited",
                    {
                        "worker_name": worker_name,
                        "child_pid": 0,
                        "supervisor_pid": observed["supervisor_pid"],
                        "tmux_target": observed["tmux_target"],
                        "exit_code": observed["exit_code"],
                        "finished_at": now_iso(),
                        "explicit_stop": True,
                    },
                )
                self.store.append(
                    run_id,
                    "worker.stopped",
                    {
                        "worker_name": worker_name,
                        "child_pid": 0,
                        "supervisor_pid": observed["supervisor_pid"],
                        "tmux_target": observed["tmux_target"],
                        "exit_code": observed["exit_code"],
                        "finished_at": now_iso(),
                        "explicit_stop": True,
                        "failure_reason": worker.failure_reason,
                        "preserve_status": True,
                    },
                )
                if worker.mcp_token_id:
                    revoke_worker_mcp_session(
                        worker.mcp_token_id,
                        run_id=run_id,
                        reason="reconcile_blocked",
                    )

        return self.get_run(run_id, rebuild=True)

    def _apply_worker_watchdog_policies_local(
        self,
        run_id: str,
        *,
        worker_names: list[str] | None = None,
    ) -> RunProjection:
        projection = self.get_run(run_id, rebuild=True)
        adapter = GitWorkspaceRuntimeAdapter(projection.run.repo_root)
        config = load_config()
        block_after = max(1.0, float(config.worker_block_after))
        retry_limit = max(1, int(config.worker_tool_retry_limit))
        remediation_limit = max(0, int(config.worker_auto_remediation_limit))
        restart_limit = max(0, int(config.worker_auto_restart_limit))

        for worker in _selected_workers(projection, worker_names):
            open_intervention = self._active_worker_intervention(projection, worker.worker_name)
            if open_intervention and (
                worker.status == WorkerStatus.running
                or self._worker_has_visible_result(worker)
                or worker.last_tool_status == "completed"
            ):
                self._resolve_worker_interventions(
                    run_id,
                    worker_name=worker.worker_name,
                    resolution_reason="worker_recovered",
                )
                projection = self.get_run(run_id, rebuild=True)
                worker = projection.workers.get(worker.worker_name, worker)
                open_intervention = None

            if worker.status == WorkerStatus.failed:
                if open_intervention is None:
                    self._open_worker_intervention(
                        worker,
                        reason=worker.failure_reason or "worker failed and requires manual intervention",
                        recommended_action="restart_worker",
                    )
                continue
            if worker.status in {WorkerStatus.superseded, WorkerStatus.stopped}:
                continue
            if worker.status == WorkerStatus.blocked:
                if open_intervention is None:
                    self._open_worker_intervention(
                        worker,
                        reason=worker.blocked_reason or worker.failure_reason or "worker is blocked",
                        recommended_action=(
                            "open_review" if self._worker_has_visible_result(worker) else "restart_worker"
                        ),
                    )
                self._request_worker_shutdown(worker)
                continue
            if worker.last_tool_status != "failed":
                continue

            current_diff_signature = self._workspace_diff_signature(adapter, worker.workspace_path)
            no_new_diff = current_diff_signature == worker.last_failed_diff_signature
            failure_age = seconds_since(worker.last_tool_at) or 0.0
            retry_exceeded = worker.tool_retry_count > retry_limit
            classification = self._classify_worker_remediation(worker)

            if self._worker_has_visible_result(worker) or worker.last_tool_name == "worker.report_result":
                intervention = self._open_worker_intervention(
                    worker,
                    reason=(
                        f"tool '{worker.last_tool_name}' failed after review evidence already existed; "
                        "manual review is required"
                    ),
                    recommended_action="open_review",
                )
                self._block_worker(
                    worker,
                    reason=intervention.reason,
                    current_diff_signature=current_diff_signature,
                    intervention_id=intervention.id,
                )
                continue

            if (
                classification
                and no_new_diff
                and worker.remediation_attempt_count < remediation_limit
                and (
                    not worker.last_remediation_at
                    or worker.last_remediation_at < worker.last_tool_at
                )
            ):
                if self._attempt_worker_remediation(
                    worker,
                    classification,
                    restart_limit=restart_limit,
                ):
                    projection = self.get_run(run_id, rebuild=True)
                    continue

            if retry_exceeded:
                intervention = self._open_worker_intervention(
                    worker,
                    reason=(
                        f"tool '{worker.last_tool_name}' failed {worker.tool_retry_count} times; "
                        "manual intervention required"
                    ),
                    recommended_action="restart_worker",
                )
                self._block_worker(
                    worker,
                    reason=intervention.reason,
                    current_diff_signature=current_diff_signature,
                    intervention_id=intervention.id,
                )
                continue

            if failure_age >= block_after and no_new_diff:
                recommended_action = "restart_worker"
                if classification and worker.restart_attempt_count >= restart_limit > 0:
                    recommended_action = "open_review"
                intervention = self._open_worker_intervention(
                    worker,
                    reason=(
                        f"tool '{worker.last_tool_name}' failed and no progress was detected for "
                        f"{int(failure_age)}s; manual intervention required"
                    ),
                    recommended_action=recommended_action,
                )
                self._block_worker(
                    worker,
                    reason=intervention.reason,
                    current_diff_signature=current_diff_signature,
                    intervention_id=intervention.id,
                )

        return self.get_run(run_id, rebuild=True)

    def create_archive(
        self,
        run_id: str,
        *,
        label: str = "",
        summary: str = "",
        actor: str = "",
    ) -> tuple[StageArchive, ApprovalGate]:
        projection = self.reconcile_workers(run_id)
        if projection.run.needs_replan:
            raise BranchClawError(f"Run '{run_id}' requires replan before archiving")
        if projection.run.status not in {
            RunStatus.executing,
            RunStatus.merge_blocked,
            RunStatus.awaiting_plan_approval,
        }:
            raise BranchClawError(
                f"Run '{run_id}' is in status '{projection.run.status.value}', cannot archive now"
            )
        self._ensure_workers_safe_for_archive(projection)

        adapter = GitWorkspaceRuntimeAdapter(projection.run.repo_root)
        workspace_snapshots = []
        for worker in projection.workers.values():
            if worker.status == WorkerStatus.superseded:
                continue
            workspace_snapshots.append(
                adapter.snapshot_workspace(
                    worker_name=worker.worker_name,
                    stage_id=worker.stage_id,
                    feature_id=worker.feature_id,
                    workspace_path=worker.workspace_path,
                    branch=worker.branch,
                    base_ref=worker.base_ref,
                    backend=worker.backend,
                    task=worker.task,
                    result=worker.result,
                )
            )

        archive = StageArchive(
            id=new_id("archive-"),
            run_id=run_id,
            stage_id=projection.run.current_stage_id,
            label=label,
            summary=summary,
            status=ArchiveStatus.pending_approval,
            state_snapshot=self._snapshot_control_state(projection),
            workspaces=workspace_snapshots,
        )
        gate = ApprovalGate(
            id=new_id("gate-"),
            run_id=run_id,
            stage_id=projection.run.current_stage_id,
            gate_type=ApprovalType.archive,
            target_id=archive.id,
        )

        self.store.append(run_id, "archive.requested", {"archive": json.loads(archive.model_dump_json())})
        self.store.append(run_id, "approval.requested", {"gate": json.loads(gate.model_dump_json())})
        self._write_archive_artifact(run_id, archive)
        return archive, gate

    def list_archives(self, run_id: str) -> list[StageArchive]:
        projection = self.get_run(run_id)
        return sorted(projection.archives.values(), key=lambda item: item.created_at)

    def request_merge(
        self,
        run_id: str,
        *,
        archive_id: str = "",
        batch_id: str = "",
        actor: str = "",
        target_ref: str = "",
    ) -> ApprovalGate:
        projection = self.reconcile_workers(run_id)
        if projection.run.needs_replan:
            raise BranchClawError(f"Run '{run_id}' requires replan before merge")
        target_id = ""
        merge_payload: dict[str, Any]
        if batch_id:
            batch = self._require_batch(projection, batch_id)
            if batch.status not in {BatchStatus.pending_approval, BatchStatus.integration_failed}:
                raise BranchClawError(
                    f"Batch '{batch.id}' is in status '{batch.status.value}', cannot request merge"
                )
            target_id = batch.id
            merge_payload = {
                "batch_id": batch.id,
                "target_ref": projection.run.integration_ref,
                "actor": actor,
            }
        else:
            self._ensure_no_live_workers(projection, action="merge")
            archive = self._require_archive(projection, archive_id)
            if archive.status != ArchiveStatus.approved:
                raise BranchClawError(f"Archive '{archive.id}' is not approved")
            target_id = archive.id
            merge_payload = {
                "archive_id": archive.id,
                "target_ref": target_ref or projection.run.base_ref,
                "actor": actor,
            }
        gate = ApprovalGate(
            id=new_id("gate-"),
            run_id=run_id,
            stage_id=projection.run.current_stage_id,
            gate_type=ApprovalType.merge,
            target_id=target_id,
        )
        self.store.append(run_id, "merge.requested", merge_payload)
        self.store.append(run_id, "approval.requested", {"gate": json.loads(gate.model_dump_json())})
        return gate

    def request_promote(
        self,
        run_id: str,
        *,
        batch_id: str,
        actor: str = "",
    ) -> ApprovalGate:
        projection = self.get_run(run_id, rebuild=True)
        batch = self._require_batch(projection, batch_id)
        if batch.status != BatchStatus.pending_promote:
            raise BranchClawError(
                f"Batch '{batch.id}' is in status '{batch.status.value}', cannot request promote"
            )
        gate = ApprovalGate(
            id=new_id("gate-"),
            run_id=run_id,
            stage_id=batch.stage_id,
            gate_type=ApprovalType.promote,
            target_id=batch.id,
        )
        self.store.append(
            run_id,
            "approval.requested",
            {"gate": json.loads(gate.model_dump_json())},
        )
        return gate

    def request_restore(
        self,
        run_id: str,
        archive_id: str,
        *,
        actor: str = "",
    ) -> ApprovalGate:
        projection = self.reconcile_workers(run_id)
        archive = self._require_archive(projection, archive_id)
        if archive.status != ArchiveStatus.approved:
            raise BranchClawError(f"Archive '{archive.id}' is not approved")
        gate = ApprovalGate(
            id=new_id("gate-"),
            run_id=run_id,
            stage_id=archive.stage_id,
            gate_type=ApprovalType.rollback,
            target_id=archive.id,
        )
        self.store.append(
            run_id,
            "rollback.requested",
            {"archive_id": archive.id, "actor": actor},
        )
        self.store.append(run_id, "approval.requested", {"gate": json.loads(gate.model_dump_json())})
        return gate

    def migrate_from_clawteam(
        self,
        team_name: str,
        *,
        new_run_name: str = "",
        clawteam_data_dir: str | None = None,
        repo: str | None = None,
    ) -> RunProjection:
        from clawteam.team.models import get_data_dir as get_clawteam_data_dir

        old_root = Path(clawteam_data_dir) if clawteam_data_dir else get_clawteam_data_dir()
        team_dir = old_root / "teams" / team_name
        config_path = team_dir / "config.json"
        if not config_path.exists():
            raise BranchClawError(f"ClawTeam team '{team_name}' not found in {old_root}")

        config = json.loads(config_path.read_text(encoding="utf-8"))
        tasks_dir = old_root / "tasks" / team_name
        task_payloads = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(tasks_dir.glob("task-*.json"))
        ] if tasks_dir.exists() else []
        event_payloads = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((team_dir / "events").glob("evt-*.json"))
        ] if (team_dir / "events").exists() else []

        projection = self.create_run(
            new_run_name or team_name,
            description=f"Imported from ClawTeam team '{team_name}'",
            spec_content=json.dumps(config, indent=2, ensure_ascii=False),
            rules_content="Imported from legacy ClawTeam state.",
            repo=repo,
        )
        art_dir = artifacts_dir(projection.run.id)
        save_json(art_dir / "legacy-team-config.json", config)
        save_json(art_dir / "legacy-tasks.json", task_payloads)
        save_json(art_dir / "legacy-events.json", event_payloads)
        self.store.append(
            projection.run.id,
            "run.migrated_from_clawteam",
            {
                "team_name": team_name,
                "task_count": len(task_payloads),
                "event_count": len(event_payloads),
            },
        )
        return self.get_run(projection.run.id)

    def export_events(self, run_id: str, *, include_heartbeats: bool = False) -> dict[str, Any]:
        return self.store.export(run_id, include_heartbeats=include_heartbeats)

    def _execute_merge(self, run_id: str, archive_id: str) -> None:
        projection = self.reconcile_workers(run_id)
        if projection.run.needs_replan:
            raise BranchClawError(f"Run '{run_id}' requires replan before merge")
        if archive_id.startswith("batch-"):
            self._execute_batch_merge(run_id, archive_id)
            return
        self._ensure_no_live_workers(projection, action="merge")
        archive = self._require_archive(projection, archive_id)
        adapter = GitWorkspaceRuntimeAdapter(projection.run.repo_root)
        failures: list[dict[str, str]] = []
        for snapshot in archive.workspaces:
            ok, output = adapter.promote_workspace(snapshot, projection.run.base_ref)
            if not ok:
                failures.append({"worker": snapshot.worker_name, "output": output})
        if failures:
            self.store.append(run_id, "merge.blocked", {"archive_id": archive_id, "failures": failures})
            return
        self.store.append(
            run_id,
            "merge.completed",
            {"archive_id": archive_id, "stage_id": archive.stage_id},
        )

    def _execute_batch_merge(self, run_id: str, batch_id: str) -> None:
        projection = self.reconcile_workers(run_id)
        batch = self._require_batch(projection, batch_id)
        self.store.append(
            run_id,
            "batch.approved",
            {"batch_id": batch.id, "approved_at": now_iso()},
        )

        adapter = GitWorkspaceRuntimeAdapter(projection.run.repo_root)
        failures: list[dict[str, str]] = []
        try:
            adapter.prepare_branch(projection.run.integration_ref, projection.run.base_ref)
            for feature_id in batch.feature_ids:
                feature = projection.features.get(feature_id)
                if feature is None:
                    failures.append({"feature": feature_id, "output": "feature not found"})
                    continue
                source_branch = feature.snapshot_branch or self._feature_branch_from_worker(projection, feature)
                if not source_branch:
                    failures.append({"feature": feature.title, "output": "missing snapshot branch"})
                    continue
                ok, output = adapter.promote_workspace_branch(source_branch, projection.run.integration_ref)
                if not ok:
                    failures.append({"feature": feature.title, "output": output})

            if failures:
                blocker = "; ".join(f"{item['feature']}: {item['output']}" for item in failures)
                self.store.append(
                    run_id,
                    "batch.integration_failed",
                    {
                        "batch_id": batch.id,
                        "validation_command": "",
                        "validation_output": blocker,
                        "blocker": blocker,
                    },
                )
                return

            validation = self._run_integration_validation(projection)
            if not validation["ok"]:
                self.store.append(
                    run_id,
                    "batch.integration_failed",
                    {
                        "batch_id": batch.id,
                        "validation_command": validation["command"],
                        "validation_output": validation["output"],
                        "blocker": validation["output"],
                    },
                )
                return

            self.store.append(
                run_id,
                "batch.integration_validated",
                {
                    "batch_id": batch.id,
                    "validation_command": validation["command"],
                    "validation_output": validation["output"],
                },
            )
            self.store.append(
                run_id,
                "merge.completed",
                {"batch_id": batch.id, "stage_id": batch.stage_id},
            )
        finally:
            adapter.checkout_ref(projection.run.base_ref)

    def _execute_promote(self, run_id: str, batch_id: str) -> None:
        projection = self.get_run(run_id, rebuild=True)
        batch = self._require_batch(projection, batch_id)
        if batch.status != BatchStatus.pending_promote:
            raise BranchClawError(
                f"Batch '{batch.id}' is in status '{batch.status.value}', cannot promote"
            )
        adapter = GitWorkspaceRuntimeAdapter(projection.run.repo_root)
        try:
            ok, output = adapter.promote_workspace_branch(projection.run.integration_ref, projection.run.base_ref)
            if not ok:
                self.store.append(
                    run_id,
                    "merge.blocked",
                    {"batch_id": batch.id, "failures": [{"batch": batch.id, "output": output}]},
                )
                return
            self.store.append(
                run_id,
                "batch.promoted",
                {"batch_id": batch.id, "promoted_at": now_iso(), "output": output},
            )
        finally:
            adapter.checkout_ref(projection.run.base_ref)

    def _execute_restore(self, run_id: str, archive_id: str) -> None:
        projection = self.reconcile_workers(run_id)
        archive = self._require_archive(projection, archive_id)
        adapter = GitWorkspaceRuntimeAdapter(projection.run.repo_root)

        for worker in projection.workers.values():
            if worker.status in LIVE_WORKER_STATUSES:
                self._request_worker_shutdown(worker)
            if worker.status != WorkerStatus.superseded:
                self.store.append(run_id, "worker.superseded", {"worker_name": worker.worker_name})
            if worker.mcp_token_id:
                revoke_worker_mcp_session(
                    worker.mcp_token_id,
                    run_id=run_id,
                    reason="restore_superseded",
                )

        projection = self.get_run(run_id, rebuild=True)
        for snapshot in archive.workspaces:
            restored = adapter.restore_workspace(run_id, archive.id, snapshot)
            worker = WorkerRuntime(
                worker_name=snapshot.worker_name,
                run_id=run_id,
                stage_id=snapshot.stage_id,
                feature_id=snapshot.feature_id,
                workspace_path=restored.workspace_path,
                branch=restored.branch,
                base_ref=restored.base_ref,
                head_sha=restored.head_sha,
                backend=snapshot.backend or "restored",
                pid=0,
                child_pid=0,
                supervisor_pid=0,
                tmux_target="",
                task=snapshot.task,
                heartbeat_at=now_iso(),
                last_heartbeat_at=now_iso(),
                started_at=now_iso(),
                finished_at=now_iso(),
                status=WorkerStatus.stopped,
                result=snapshot.result,
            )
            self.store.append(run_id, "worker.started", {"worker": json.loads(worker.model_dump_json())})

        self.store.append(run_id, "archive.restored", {"archive_id": archive.id})

    def _require_archive(self, projection: RunProjection, archive_id: str) -> StageArchive:
        if archive_id:
            archive = projection.archives.get(archive_id)
            if archive is None:
                raise BranchClawError(f"Archive '{archive_id}' not found")
            return archive
        approved = [archive for archive in projection.archives.values() if archive.status == ArchiveStatus.approved]
        if not approved:
            raise BranchClawError("No approved archive available")
        approved.sort(key=lambda item: item.created_at, reverse=True)
        return approved[0]

    def _require_batch(self, projection: RunProjection, batch_id: str) -> BatchRecord:
        batch = projection.batches.get(batch_id)
        if batch is None:
            raise BranchClawError(f"Batch '{batch_id}' not found")
        return batch

    def _sync_features_from_plan(self, run_id: str, plan: PlanProposal) -> None:
        projection = self.get_run(run_id, rebuild=True)
        specs = self._parse_plan_features(plan.content)
        if not specs:
            return
        existing_by_key = {
            self._feature_key(feature.title): feature
            for feature in projection.features.values()
            if feature.status != FeatureStatus.merged
        }
        seen: set[str] = set()
        for spec in specs:
            key = self._feature_key(str(spec.get("title", "")))
            if not key:
                continue
            seen.add(key)
            current = existing_by_key.get(key)
            if current is None:
                feature = FeatureRecord(
                    id=new_id("feature-"),
                    run_id=run_id,
                    stage_id=projection.run.current_stage_id,
                    title=str(spec.get("title", "")).strip(),
                    goal=str(spec.get("goal", "")).strip(),
                    task=str(spec.get("task", "")).strip() or str(spec.get("goal", "")).strip(),
                    claimed_areas=list(spec.get("claimed_areas", [])),
                    claimed_files=list(spec.get("claimed_files", [])),
                    priority=int(spec.get("priority", 100)),
                    validation_command=str(spec.get("validation_command", "")).strip(),
                )
                self.store.append(
                    run_id,
                    "feature.created",
                    {"feature": json.loads(feature.model_dump_json())},
                )
                continue

            updated = current.model_copy(
                update={
                    "stage_id": projection.run.current_stage_id,
                    "goal": str(spec.get("goal", current.goal)).strip(),
                    "task": str(spec.get("task", current.task)).strip() or current.task,
                    "claimed_areas": list(spec.get("claimed_areas", current.claimed_areas)),
                    "claimed_files": list(spec.get("claimed_files", current.claimed_files)),
                    "priority": int(spec.get("priority", current.priority)),
                    "validation_command": str(
                        spec.get("validation_command", current.validation_command)
                    ).strip(),
                    "updated_at": now_iso(),
                }
            )
            self.store.append(
                run_id,
                "feature.updated",
                {"feature": json.loads(updated.model_dump_json())},
            )

        for feature in projection.features.values():
            key = self._feature_key(feature.title)
            if (
                key
                and key not in seen
                and feature.status == FeatureStatus.queued
            ):
                dropped = feature.model_copy(
                    update={
                        "status": FeatureStatus.dropped,
                        "updated_at": now_iso(),
                    }
                )
                self.store.append(
                    run_id,
                    "feature.updated",
                    {"feature": json.loads(dropped.model_dump_json())},
                )

    def _parse_plan_features(self, content: str) -> list[dict[str, Any]]:
        if not content.strip():
            return []
        json_match = re.search(
            r"```(?:json|branchclaw-features)\s*(\[.*?\])\s*```",
            content,
            flags=re.DOTALL,
        )
        if json_match:
            try:
                raw = json.loads(json_match.group(1))
            except Exception:
                raw = []
            if isinstance(raw, list):
                return [self._normalize_feature_spec(item) for item in raw if isinstance(item, dict)]

        sections = re.split(r"(?m)^##+\s*Feature\s*:\s*", content)
        specs: list[dict[str, Any]] = []
        for section in sections[1:]:
            title, _, body = section.partition("\n")
            rows: dict[str, str] = {}
            for line in body.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                rows[key.strip().lower()] = value.strip()
            specs.append(
                self._normalize_feature_spec(
                    {
                        "title": title.strip(),
                        "goal": rows.get("goal", ""),
                        "task": rows.get("task", ""),
                        "claimed_areas": rows.get("areas", ""),
                        "claimed_files": rows.get("files", ""),
                        "priority": rows.get("priority", "100"),
                        "validation_command": rows.get("validation", ""),
                    }
                )
            )
        return [spec for spec in specs if spec.get("title")]

    def _normalize_feature_spec(self, payload: dict[str, Any]) -> dict[str, Any]:
        def _listify(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
            return [
                item.strip()
                for item in str(value or "").replace("\n", ",").split(",")
                if item.strip()
            ]

        try:
            priority = int(payload.get("priority", 100))
        except (TypeError, ValueError):
            priority = 100
        return {
            "title": str(payload.get("title", "")).strip(),
            "goal": str(payload.get("goal", "")).strip(),
            "task": str(payload.get("task", "")).strip(),
            "claimed_areas": _listify(payload.get("claimed_areas") or payload.get("areas")),
            "claimed_files": _listify(payload.get("claimed_files") or payload.get("files")),
            "priority": priority,
            "validation_command": str(payload.get("validation_command", "")).strip(),
        }

    def _refresh_feature_state(self, projection: RunProjection) -> RunProjection:
        for feature in projection.features.values():
            if feature.status in {FeatureStatus.merged, FeatureStatus.dropped}:
                continue
            worker = projection.workers.get(feature.worker_name) if feature.worker_name else None
            if worker is None:
                continue

            desired = feature.model_copy()
            desired.snapshot_branch = worker.branch or desired.snapshot_branch
            desired.snapshot_head_sha = worker.head_sha or desired.snapshot_head_sha
            desired.snapshot_workspace_path = worker.workspace_path or desired.snapshot_workspace_path
            desired.updated_at = now_iso()

            if worker.status in LIVE_WORKER_STATUSES and feature.status in {
                FeatureStatus.assigned,
                FeatureStatus.blocked,
            }:
                desired.status = FeatureStatus.in_progress
                desired.integration_blocker = ""

            if worker.result and worker.result.status.value == "success":
                validation = self._validate_feature(worker.workspace_path, desired)
                desired.validation_status = ValidationStatus(validation["status"])
                desired.validation_command = validation["command"]
                desired.validation_output = validation["output"]
                desired.validation_ran_at = validation["ran_at"]
                desired.result = worker.result
                desired.result_summary = (
                    worker.result.changed_surface_summary
                    or worker.result.output_snippet
                    or worker.result.architecture_summary[:240]
                )
                desired.snapshot_recorded_at = now_iso()
                if desired.validation_status == ValidationStatus.passed:
                    desired.status = FeatureStatus.ready
                    desired.integration_blocker = ""
                    if worker.status in LIVE_WORKER_STATUSES:
                        self._request_worker_shutdown(worker)
                else:
                    desired.status = FeatureStatus.blocked
                    desired.integration_blocker = validation["output"] or "feature validation failed"
            elif worker.status in {WorkerStatus.failed, WorkerStatus.blocked}:
                desired.status = FeatureStatus.blocked
                desired.integration_blocker = worker.failure_reason or worker.blocked_reason or desired.integration_blocker

            if json.loads(desired.model_dump_json()) != json.loads(feature.model_dump_json()):
                event_type = "feature.ready" if desired.status == FeatureStatus.ready else (
                    "feature.blocked" if desired.status == FeatureStatus.blocked else "feature.updated"
                )
                self.store.append(
                    projection.run.id,
                    event_type,
                    {"feature": json.loads(desired.model_dump_json())},
                )
                projection = self.get_run(projection.run.id, rebuild=True)
        return projection

    def _ensure_ready_batch(self, projection: RunProjection) -> None:
        open_batches = [
            batch
            for batch in projection.batches.values()
            if batch.status in {
                BatchStatus.pending_approval,
                BatchStatus.integrating,
                BatchStatus.integration_failed,
                BatchStatus.pending_promote,
            }
        ]
        if open_batches:
            return
        ready = sorted(
            (
                feature
                for feature in projection.features.values()
                if feature.status == FeatureStatus.ready
            ),
            key=lambda item: (item.priority, item.created_at),
        )
        if not ready:
            return
        batch = BatchRecord(
            id=new_id("batch-"),
            run_id=projection.run.id,
            stage_id=projection.run.current_stage_id,
            feature_ids=[feature.id for feature in ready],
            status=BatchStatus.pending_approval,
            integration_ref=projection.run.integration_ref,
        )
        self.store.append(
            projection.run.id,
            "batch.proposed",
            {"batch": json.loads(batch.model_dump_json())},
        )

    def _feature_conflicts(
        self,
        feature: FeatureRecord,
        claimed_areas: set[str],
        claimed_files: set[str],
    ) -> bool:
        if feature.claimed_files and set(feature.claimed_files) & claimed_files:
            return True
        if feature.claimed_areas and set(feature.claimed_areas) & claimed_areas:
            return True
        return False

    def _feature_worker_name(self, feature: FeatureRecord) -> str:
        return f"feature-{_slug(feature.title)[:24]}-{feature.id[-4:]}"

    def _feature_key(self, title: str) -> str:
        return _slug(title).lower()

    def _feature_branch_from_worker(self, projection: RunProjection, feature: FeatureRecord) -> str:
        worker = projection.workers.get(feature.worker_name) if feature.worker_name else None
        return worker.branch if worker is not None else ""

    def _validate_feature(self, workspace_path: str, feature: FeatureRecord) -> dict[str, str]:
        command = feature.validation_command.strip()
        if not command:
            return {
                "status": ValidationStatus.passed.value,
                "command": "",
                "output": "",
                "ran_at": now_iso(),
            }
        completed = subprocess.run(
            shlex.split(command),
            cwd=workspace_path,
            capture_output=True,
            text=True,
        )
        output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
        return {
            "status": ValidationStatus.passed.value if completed.returncode == 0 else ValidationStatus.failed.value,
            "command": command,
            "output": output,
            "ran_at": now_iso(),
        }

    def _run_integration_validation(self, projection: RunProjection) -> dict[str, str | bool]:
        command = self._default_integration_validation_command(projection)
        if not command:
            return {"ok": True, "command": "", "output": ""}
        completed = subprocess.run(
            shlex.split(command),
            cwd=projection.run.repo_root,
            capture_output=True,
            text=True,
        )
        output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
        return {
            "ok": completed.returncode == 0,
            "command": command,
            "output": output,
        }

    def _default_integration_validation_command(self, projection: RunProjection) -> str:
        info = detect_project_stack(projection.run.repo_root)
        runtime = str(info.get("runtime", ""))
        if runtime == "node":
            package_json = Path(projection.run.repo_root) / "package.json"
            try:
                package = json.loads(package_json.read_text(encoding="utf-8"))
            except Exception:
                package = {}
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            package_manager = str(info.get("package_manager") or "npm")
            if "build" in scripts:
                return f"{package_manager} run build"
            if "test" in scripts:
                return f"{package_manager} run test"
            return ""
        if runtime == "python":
            if (Path(projection.run.repo_root) / "pyproject.toml").exists():
                return "pytest -q"
            if (Path(projection.run.repo_root) / "requirements.txt").exists():
                return "pytest -q"
        return ""

    def _snapshot_control_state(self, projection: RunProjection) -> dict[str, Any]:
        return {
            "runStatus": projection.run.status.value,
            "projectProfile": projection.run.project_profile.value,
            "activePlanId": projection.run.active_plan_id,
            "constraintIds": [item.id for item in projection.constraints],
            "workerNames": list(projection.workers.keys()),
            "featureIds": list(projection.features.keys()),
            "batchIds": list(projection.batches.keys()),
            "needsReplan": projection.run.needs_replan,
            "dirtyReason": projection.run.dirty_reason,
        }

    def _build_worker_prompt(
        self,
        projection: RunProjection,
        worker_name: str,
        task: str,
        *,
        mcp_enabled: bool,
    ) -> str:
        bundle = self.compile_execution_bundle(projection)
        parts = [
            f"# Worker Identity\n- Name: {worker_name}\n- Run: {projection.run.id}",
            "",
            bundle,
            "",
            render_project_skill_prompt(
                projection.run.project_profile,
                mcp_enabled=mcp_enabled,
            ),
        ]
        if task:
            parts.extend(["", "# Task", task])
        if mcp_enabled:
            parts.extend(
                [
                    "",
                    "# MCP Execution Contract",
                    "This session already has BranchClaw MCP tools configured.",
                    "Use those MCP tools as the primary way to inspect context, install dependencies, start services, discover preview URLs, generate architecture summaries, checkpoint work, and report your result.",
                    "These are native tools already attached to the session, not shell commands.",
                    "Do not try to run `mcp call ...`, `branchclaw worker report`, or similar Bash commands unless the MCP tools are unavailable.",
                    "Do not rely on a fixed script order. Choose the next tool call based on the current repo state and task progress.",
                    "Before stopping, call `worker.report_result` with your final structured outcome.",
                ]
            )
        return "\n".join(parts)

    def _build_worker_system_prompt(
        self,
        projection: RunProjection,
        worker_name: str,
        task: str,
        *,
        mcp_enabled: bool,
    ) -> str:
        if not mcp_enabled:
            return ""
        return "\n".join(
            [
                "BranchClaw has attached native MCP tools to this session for project/runtime work.",
                f"You are worker '{worker_name}' in run '{projection.run.id}' on project profile '{projection.run.project_profile.value}'.",
                "Treat the following as native session tools, not shell commands:",
                "- context.get_worker_context",
                "- project.detect",
                "- project.install_dependencies",
                "- service.start_tmux",
                "- service.discover_url",
                "- service.stop_tmux",
                "- diff.generate_architecture_summary",
                "- worker.create_checkpoint",
                "- worker.report_result",
                "Before making project/runtime decisions, call context.get_worker_context.",
                "When one of the tools above can do the job, use the tool instead of Bash, helper scripts, or `branchclaw` CLI commands.",
                "Do not run invented shell commands such as `mcp call ...`.",
                "Only use helper scripts or shell fallbacks if the native MCP tools are unavailable or insufficient, and say so explicitly.",
                "Do not claim completion until you have called worker.report_result or explicitly established that the MCP tools are unavailable.",
                f"Current task: {task}" if task else "Current task: follow the latest worker task from context.get_worker_context.",
            ]
        )

    def _planner_dirty_context(self, projection: RunProjection) -> str:
        if not projection.run.needs_replan:
            return ""
        return "\n".join(
            [
                "Dirty Planner State:",
                f"- Reason: {projection.run.dirty_reason or 'constraint'}",
                f"- Since: {projection.run.dirty_since or '(unknown)'}",
                f"- Stage: {projection.run.dirty_stage_id or projection.run.current_stage_id}",
                f"- Latest Constraint: {projection.run.latest_constraint_id or '(none)'}",
                f"- Active Plan: {projection.run.active_plan_id or '(none)'}",
            ]
        )

    def _write_plan_artifact(self, run_id: str, plan: PlanProposal) -> None:
        path = artifacts_dir(run_id) / "plans" / f"{plan.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.content, encoding="utf-8")
        bundle_path = artifacts_dir(run_id) / "plans" / f"{plan.id}.bundle.md"
        bundle_path.write_text(plan.effective_bundle, encoding="utf-8")

    def _write_archive_artifact(self, run_id: str, archive: StageArchive) -> None:
        path = artifacts_dir(run_id) / "archives" / f"{archive.id}.json"
        save_json(path, json.loads(archive.model_dump_json()))

    def _write_worker_result_artifact(self, run_id: str, worker_name: str, result: WorkerResult) -> None:
        path = artifacts_dir(run_id) / "worker-results" / f"{_slug(worker_name)}.json"
        save_json(path, json.loads(result.model_dump_json()))

    def _await_worker_start(self, run_id: str, worker_name: str, supervisor_pid: int) -> WorkerRuntime:
        timeout = max(1.0, load_config().supervisor_start_timeout)
        launch_path = worker_launch_path(run_id, worker_name)
        if launch_path.exists():
            try:
                payload = json.loads(launch_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            timeout = max(timeout, float(payload.get("claude_ready_timeout", 0.0)) + 5.0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = read_worker_status(run_id, worker_name)
            if status.get("status") == "launch_failed":
                raise BranchClawError(status.get("error") or "Worker failed to launch")
            projection = self.get_run(run_id, rebuild=True)
            worker = projection.workers.get(worker_name)
            if worker:
                return worker
            if supervisor_pid and not pid_alive(supervisor_pid):
                raise BranchClawError("Supervisor exited before worker started")
            time.sleep(0.1)
        raise BranchClawError(f"Timed out waiting for worker '{worker_name}' to start")

    def _safe_head_sha(self, adapter: GitWorkspaceRuntimeAdapter, workspace_path: str) -> str:
        try:
            return adapter.head_sha(workspace_path)
        except Exception:
            return ""

    def _workspace_diff_signature(
        self,
        adapter: GitWorkspaceRuntimeAdapter,
        workspace_path: str,
    ) -> str:
        try:
            return adapter.diff_signature(workspace_path)
        except Exception:
            return ""

    def _worker_has_visible_result(self, worker: WorkerRuntime) -> bool:
        if worker.discovered_url:
            return True
        if worker.result is None:
            return False
        return any(
            [
                worker.result.preview_url,
                worker.result.backend_url,
                worker.result.output_snippet,
                worker.result.changed_surface_summary,
                worker.result.architecture_summary,
            ]
        ) or bool(worker.report_source)

    def _current_worktree_entry_id(self, worker: WorkerRuntime) -> str:
        kind = "restored" if "/restored/" in worker.workspace_path else "current"
        return worktree_entry_id(
            worker_name=worker.worker_name,
            kind=kind,
            stage_id=worker.stage_id,
            archive_id="",
            workspace_path=worker.workspace_path,
            head_sha=worker.head_sha,
            recorded_at=worker.started_at,
        )

    def _active_worker_intervention(
        self,
        projection: RunProjection,
        worker_name: str,
    ) -> WorkerIntervention | None:
        for intervention in projection.interventions.values():
            if (
                intervention.worker_name == worker_name
                and intervention.status == InterventionStatus.open
            ):
                return intervention
        return None

    def _open_worker_intervention(
        self,
        worker: WorkerRuntime,
        *,
        reason: str,
        recommended_action: str,
    ) -> WorkerIntervention:
        projection = self.get_run(worker.run_id, rebuild=True)
        existing = self._active_worker_intervention(projection, worker.worker_name)
        related_entry_id = self._current_worktree_entry_id(worker)
        if (
            existing is not None
            and existing.reason == reason
            and existing.recommended_action == recommended_action
            and existing.related_entry_id == related_entry_id
        ):
            return existing
        if existing is not None:
            self.store.append(
                worker.run_id,
                "worker.intervention_resolved",
                {
                    "intervention_id": existing.id,
                    "worker_name": worker.worker_name,
                    "resolved_at": now_iso(),
                    "resolution_reason": "superseded",
                },
            )
        intervention = WorkerIntervention(
            id=new_id("intr-"),
            run_id=worker.run_id,
            worker_name=worker.worker_name,
            feature_id=worker.feature_id,
            reason=reason,
            recommended_action=recommended_action,
            last_tool_name=worker.last_tool_name,
            last_tool_error=worker.last_tool_error,
            remediation_attempts=worker.remediation_attempt_count,
            restart_attempts=worker.restart_attempt_count,
            related_entry_id=related_entry_id,
        )
        self.store.append(
            worker.run_id,
            "worker.intervention_opened",
            {"intervention": json.loads(intervention.model_dump_json())},
        )
        return intervention

    def _resolve_worker_interventions(
        self,
        run_id: str,
        *,
        worker_name: str = "",
        resolution_reason: str,
    ) -> None:
        projection = self.get_run(run_id, rebuild=True)
        for intervention in projection.interventions.values():
            if intervention.status != InterventionStatus.open:
                continue
            if worker_name and intervention.worker_name != worker_name:
                continue
            self.store.append(
                run_id,
                "worker.intervention_resolved",
                {
                    "intervention_id": intervention.id,
                    "worker_name": intervention.worker_name,
                    "resolved_at": now_iso(),
                    "resolution_reason": resolution_reason,
                },
            )

    def _classify_worker_remediation(self, worker: WorkerRuntime) -> dict[str, Any] | None:
        tool_name = worker.last_tool_name
        error = (worker.last_tool_error or "").lower()
        if tool_name == "project.install_dependencies":
            if any(token in error for token in ("corepack", "pnpm", "yarn", "lockfile", "package manager", "eresolve")):
                return {
                    "action": "fallback_install_npm",
                    "recommended_action": "restart_worker",
                    "auto_restart": True,
                }
        elif tool_name == "service.start_tmux":
            return {
                "action": "reset_tmux_target",
                "recommended_action": "restart_worker",
                "auto_restart": True,
            }
        elif tool_name == "service.discover_url":
            return {
                "action": "retry_discover_url",
                "recommended_action": "restart_worker",
                "auto_restart": True,
            }
        return None

    def _attempt_worker_remediation(
        self,
        worker: WorkerRuntime,
        classification: dict[str, Any],
        *,
        restart_limit: int,
    ) -> bool:
        action = str(classification.get("action", "")).strip()
        remediation_attempts = worker.remediation_attempt_count + 1
        self.store.append(
            worker.run_id,
            "worker.remediation_attempted",
            {
                "worker_name": worker.worker_name,
                "tool_name": worker.last_tool_name,
                "action": action,
                "remediation_attempts": remediation_attempts,
                "restart_attempts": worker.restart_attempt_count,
            },
        )

        details: dict[str, Any] = {}
        try:
            if action == "fallback_install_npm":
                project_info = detect_project_stack(worker.workspace_path)
                project_info["package_manager"] = "npm"
                result = install_dependencies(worker.workspace_path, project_info)
                if not result.get("ok"):
                    raise BranchClawError(result.get("stderr") or "npm fallback install failed")
                details = {
                    "install_command": " ".join(result.get("command") or []),
                }
            elif action == "reset_tmux_target":
                target = worker.active_service_target or worker.tmux_target
                if target:
                    terminate_tmux_target(target)
                details = {"active_service_target": target}
            elif action == "retry_discover_url":
                raw_log_path = str(
                    worker.last_tool_arguments.get("log_path")
                    or worker.active_service_log_path
                    or ".branchclaw-preview.log"
                )
                log_path = Path(raw_log_path)
                if not log_path.is_absolute():
                    log_path = Path(worker.workspace_path) / log_path
                timeout_seconds = max(
                    1.0,
                    min(10.0, float(worker.last_tool_arguments.get("timeout_seconds", 5.0) or 5.0)),
                )
                discovered_url = wait_for_url(log_path, timeout_seconds=timeout_seconds)
                if not discovered_url:
                    raise BranchClawError("No preview URL discovered after remediation retry")
                details = {
                    "discovered_url": discovered_url,
                    "active_service_log_path": str(log_path),
                }
            else:
                raise BranchClawError(f"Unknown remediation action '{action}'")

            restart_attempts = worker.restart_attempt_count
            if classification.get("auto_restart"):
                if restart_attempts >= restart_limit:
                    raise BranchClawError("automatic restart budget exhausted")
                restarted = self._auto_restart_worker(worker)
                restart_attempts = restarted.restart_attempt_count
                details["auto_restarted"] = True
                details["restart_status"] = restarted.status.value
            self.store.append(
                worker.run_id,
                "worker.remediation_succeeded",
                {
                    "worker_name": worker.worker_name,
                    "tool_name": worker.last_tool_name,
                    "action": action,
                    "remediation_attempts": remediation_attempts,
                    "restart_attempts": restart_attempts,
                    **details,
                },
            )
            return True
        except Exception as exc:
            self.store.append(
                worker.run_id,
                "worker.remediation_failed",
                {
                    "worker_name": worker.worker_name,
                    "tool_name": worker.last_tool_name,
                    "action": action,
                    "error": str(exc),
                    "remediation_attempts": remediation_attempts,
                    "restart_attempts": worker.restart_attempt_count,
                },
            )
            return False

    def _block_worker(
        self,
        worker: WorkerRuntime,
        *,
        reason: str,
        current_diff_signature: str,
        intervention_id: str = "",
    ) -> None:
        if worker.status == WorkerStatus.blocked:
            self._request_worker_shutdown(worker)
            return
        blocked_at = now_iso()
        self.store.append(
            worker.run_id,
            "worker.blocked",
            {
                "worker_name": worker.worker_name,
                "blocked_at": blocked_at,
                "blocked_reason": reason,
                "tool_name": worker.last_tool_name,
                "tool_retry_count": worker.tool_retry_count,
                "last_tool_error": worker.last_tool_error,
                "last_tool_at": worker.last_tool_at,
                "failure_diff_signature": worker.last_failed_diff_signature,
                "current_diff_signature": current_diff_signature,
                "remediation_attempts": worker.remediation_attempt_count,
                "restart_attempts": worker.restart_attempt_count,
                "intervention_id": intervention_id,
                "manual_intervention_required": True,
            },
        )
        self._request_worker_shutdown(worker)

    def _auto_restart_worker(self, worker: WorkerRuntime) -> WorkerRuntime:
        self._request_worker_shutdown(worker)
        deadline = time.time() + 4.0
        while time.time() < deadline:
            projection = self.get_run(worker.run_id, rebuild=True)
            current = projection.workers.get(worker.worker_name)
            if current is not None and current.status in TERMINAL_WORKER_STATUSES:
                break
            self._reconcile_workers_local(worker.run_id, worker_names=[worker.worker_name])
            time.sleep(0.1)

        projection = self.get_run(worker.run_id, rebuild=True)
        current = projection.workers.get(worker.worker_name, worker)
        if current.status not in TERMINAL_WORKER_STATUSES:
            self._force_worker_termination(current)
            self._reconcile_workers_local(worker.run_id, worker_names=[worker.worker_name])
        return self.restart_worker(worker.run_id, worker.worker_name, auto=True)

    def _request_worker_shutdown(self, worker: WorkerRuntime) -> None:
        stop_path = worker_stop_path(worker.run_id, worker.worker_name)
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.touch(exist_ok=True)
        self._terminate_active_service_target(worker)
        if worker.supervisor_pid > 0:
            try:
                os.kill(worker.supervisor_pid, signal.SIGTERM)
                return
            except ProcessLookupError:
                pass

        if worker.backend == "tmux" and worker.tmux_target and tmux_target_alive(worker.tmux_target):
            terminate_tmux_target(worker.tmux_target)
            return

        child_pid = worker.child_pid or worker.pid
        if child_pid > 0:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _observe_worker_state(self, worker: WorkerRuntime, status: dict[str, Any]) -> dict[str, Any]:
        supervisor_pid = int(status.get("supervisor_pid") or worker.supervisor_pid or 0)
        tmux_target = status.get("tmux_target") or worker.tmux_target
        child_pid = int(status.get("child_pid") or worker.child_pid or worker.pid or 0)
        if worker.backend == "tmux":
            child_pid = tmux_target_pid(tmux_target) or child_pid
            alive = tmux_target_alive(tmux_target)
        else:
            alive = pid_alive(child_pid)
        supervisor_alive = pid_alive(supervisor_pid)
        status_value = status.get("status", "")
        explicit_stop = bool(status.get("explicit_stop"))
        failure_reason = status.get("failure_reason", "")
        exit_code = status.get("exit_code")
        if exit_code is None:
            exit_code = 0 if explicit_stop or status_value == "stopped" else 1
        return {
            "alive": alive,
            "supervisor_alive": supervisor_alive,
            "supervisor_pid": supervisor_pid,
            "child_pid": child_pid if alive else 0,
            "tmux_target": tmux_target,
            "explicit_stop": explicit_stop or status_value == "stopped",
            "failure_reason": failure_reason or (
                "" if explicit_stop or status_value == "stopped" else "worker runtime became unreachable"
            ),
            "exit_code": int(exit_code),
        }

    def _terminate_active_service_target(self, worker: WorkerRuntime) -> None:
        target = worker.active_service_target
        if not target or target == worker.tmux_target:
            return
        if tmux_target_alive(target):
            terminate_tmux_target(target)

    def _force_worker_termination(self, worker: WorkerRuntime) -> None:
        self._terminate_active_service_target(worker)
        if worker.backend == "tmux" and worker.tmux_target:
            terminate_tmux_target(worker.tmux_target)
            return
        child_pid = worker.child_pid or worker.pid
        if child_pid > 0:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _ensure_workers_safe_for_archive(self, projection: RunProjection) -> None:
        blocking = [
            worker.worker_name
            for worker in projection.workers.values()
            if worker.status in BLOCKING_WORKER_STATUSES
        ]
        if blocking:
            raise BranchClawError(
                f"Cannot archive while workers are active/blocked: {', '.join(sorted(blocking))}"
            )

    def _ensure_no_live_workers(self, projection: RunProjection, *, action: str) -> None:
        blocking = [
            worker.worker_name
            for worker in projection.workers.values()
            if worker.status in BLOCKING_WORKER_STATUSES
        ]
        if blocking:
            raise BranchClawError(
                f"Cannot {action} while workers are active/blocked: {', '.join(sorted(blocking))}"
            )


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "-" for char in value).strip("-") or "run"
