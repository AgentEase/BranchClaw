"""Unified worker runtime store and liveness helpers."""

from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clawteam.team.models import get_data_dir
from clawteam.worker_runtime.models import (
    ExecutionAttempt,
    Heartbeat,
    WorkerRecord,
    WorkerState,
    WorkspaceBinding,
    now_iso,
)


class WorkerRuntimeStore:
    """File-backed store for worker lifecycle state."""

    def __init__(self, team_name: str):
        self.team_name = team_name

    @staticmethod
    def workers_root() -> Path:
        root = get_data_dir() / "workers"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def team_root(self) -> Path:
        root = self.workers_root() / self.team_name
        root.mkdir(parents=True, exist_ok=True)
        return root

    def worker_path(self, worker_name: str) -> Path:
        return self.team_root() / f"{worker_name}.json"

    def load(self, worker_name: str) -> WorkerRecord | None:
        path = self.worker_path(worker_name)
        if not path.exists():
            return None
        try:
            return WorkerRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save(self, record: WorkerRecord) -> WorkerRecord:
        path = self.worker_path(record.worker_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        record.touch()
        _atomic_write(path, record.model_dump_json(indent=2, by_alias=True))
        return record

    def ensure_worker(
        self,
        worker_name: str,
        worker_id: str = "",
        state: WorkerState = WorkerState.created,
    ) -> WorkerRecord:
        record = self.load(worker_name)
        if record is None:
            record = WorkerRecord(
                teamName=self.team_name,
                workerName=worker_name,
                workerId=worker_id,
                state=state,
            )
        else:
            if worker_id and not record.worker_id:
                record.worker_id = worker_id
            if record.state == WorkerState.archived and state != WorkerState.archived:
                record.archived_at = ""
            if state and record.state == WorkerState.created:
                record.state = state
        return self.save(record)

    def list_workers(self) -> list[WorkerRecord]:
        items: list[WorkerRecord] = []
        for path in sorted(self.team_root().glob("*.json")):
            try:
                items.append(WorkerRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return items

    def update_worker(
        self,
        worker_name: str,
        **updates: Any,
    ) -> WorkerRecord:
        record = self.ensure_worker(worker_name)
        for key, value in updates.items():
            if value is not None:
                setattr(record, key, value)
        return self.save(record)

    def record_spawn(
        self,
        worker_name: str,
        worker_id: str = "",
        backend: str = "",
        pid: int = 0,
        tmux_target: str = "",
        command: list[str] | None = None,
        assignment: str = "",
        current_stage: str = "spawned",
        state: WorkerState = WorkerState.ready,
    ) -> WorkerRecord:
        record = self.ensure_worker(worker_name, worker_id=worker_id, state=state)
        record.worker_id = worker_id or record.worker_id
        record.spawn_backend = backend
        record.pid = pid
        record.tmux_target = tmux_target
        record.command = list(command or [])
        if assignment:
            record.assignment = assignment
        if current_stage:
            record.current_stage = current_stage
        record.state = state
        attempt = ExecutionAttempt(
            attempt=len(record.execution_attempts) + 1,
            backend=backend,
            pid=pid,
            tmuxTarget=tmux_target,
            command=list(command or []),
        )
        record.execution_attempts.append(attempt)
        if record.last_heartbeat is None:
            record.last_heartbeat = Heartbeat(
                state=state,
                assignment=record.assignment,
                currentStage=record.current_stage,
                sessionId=record.session_id,
                pid=pid,
                tmuxTarget=tmux_target,
            )
        return self.save(record)

    def bind_workspace(
        self,
        worker_name: str,
        branch: str,
        worktree_path: str,
        repo_root: str,
        base_branch: str,
        state: WorkerState | None = None,
    ) -> WorkerRecord:
        record = self.ensure_worker(worker_name)
        record.workspace = WorkspaceBinding(
            branch=branch,
            worktreePath=worktree_path,
            repoRoot=repo_root,
            baseBranch=base_branch,
        )
        if state is not None:
            record.state = state
        return self.save(record)

    def clear_workspace(self, worker_name: str) -> WorkerRecord | None:
        record = self.load(worker_name)
        if record is None:
            return None
        record.workspace = None
        return self.save(record)

    def save_session(
        self,
        worker_name: str,
        session_id: str = "",
        last_task_id: str = "",
        state_payload: dict[str, Any] | None = None,
    ) -> WorkerRecord:
        record = self.ensure_worker(worker_name)
        record.session_id = session_id or record.session_id
        record.last_task_id = last_task_id or record.last_task_id
        if state_payload:
            record.metadata.setdefault("session", {}).update(state_payload)
        return self.save(record)

    def clear_session(self, worker_name: str) -> bool:
        record = self.load(worker_name)
        if record is None:
            return False
        record.session_id = ""
        record.last_task_id = ""
        record.metadata.pop("session", None)
        self.save(record)
        return True

    def record_heartbeat(
        self,
        worker_name: str,
        state: WorkerState | None = None,
        assignment: str | None = None,
        current_stage: str | None = None,
        session_id: str | None = None,
        pid: int | None = None,
        tmux_target: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkerRecord:
        record = self.ensure_worker(worker_name)
        hb = Heartbeat(
            state=state or record.state,
            assignment=assignment if assignment is not None else record.assignment,
            currentStage=current_stage if current_stage is not None else record.current_stage,
            sessionId=session_id if session_id is not None else record.session_id,
            pid=pid if pid is not None else record.pid,
            tmuxTarget=tmux_target if tmux_target is not None else record.tmux_target,
            metadata=metadata or {},
        )
        record.last_heartbeat = hb
        if state is not None:
            record.state = state
        if assignment is not None:
            record.assignment = assignment
        if current_stage is not None:
            record.current_stage = current_stage
        if session_id is not None:
            record.session_id = session_id
        if pid is not None:
            record.pid = pid
        if tmux_target is not None:
            record.tmux_target = tmux_target
        return self.save(record)

    def mark_exited(self, worker_name: str, reason: str = "", archive: bool = False) -> WorkerRecord:
        record = self.ensure_worker(worker_name)
        record.last_exit_reason = reason
        record.current_stage = "exited"
        record.state = WorkerState.archived if archive else WorkerState.exited
        if archive:
            record.archived_at = now_iso()
        record.last_heartbeat = Heartbeat(
            state=record.state,
            assignment=record.assignment,
            currentStage=record.current_stage,
            sessionId=record.session_id,
            pid=record.pid,
            tmuxTarget=record.tmux_target,
            metadata={"exitReason": reason},
        )
        if record.execution_attempts:
            record.execution_attempts[-1].exited_at = now_iso()
            record.execution_attempts[-1].exit_reason = reason
        return self.save(record)

    def recover_worker(
        self,
        worker_name: str,
        state: WorkerState = WorkerState.ready,
        stage: str = "recovered",
        clear_assignment: bool = False,
    ) -> WorkerRecord | None:
        record = self.load(worker_name)
        if record is None:
            return None
        if clear_assignment:
            record.assignment = ""
        record.state = state
        record.current_stage = stage
        return self.save(record)

    def is_worker_alive(self, worker_name: str, stale_after_seconds: int = 120) -> bool | None:
        record = self.load(worker_name)
        if not record:
            return None

        backend = record.spawn_backend
        if backend == "tmux":
            alive = _tmux_pane_alive(record.tmux_target)
            if alive is False and record.pid:
                return _pid_alive(record.pid)
            if alive:
                return True
        elif backend == "subprocess":
            if record.pid:
                return _pid_alive(record.pid)

        if record.last_heartbeat is None:
            return None
        return not heartbeat_is_stale(record.last_heartbeat, stale_after_seconds)

    def list_dead_workers(self, stale_after_seconds: int = 120) -> list[str]:
        dead: list[str] = []
        for worker in self.list_workers():
            alive = self.is_worker_alive(worker.worker_name, stale_after_seconds=stale_after_seconds)
            if alive is False:
                dead.append(worker.worker_name)
        return dead

    def stale_workers(self, stale_after_seconds: int = 120) -> list[WorkerRecord]:
        items: list[WorkerRecord] = []
        for worker in self.list_workers():
            if worker.last_heartbeat and heartbeat_is_stale(worker.last_heartbeat, stale_after_seconds):
                items.append(worker)
        return items


def heartbeat_is_stale(heartbeat: Heartbeat, stale_after_seconds: int) -> bool:
    try:
        recorded = datetime.fromisoformat(heartbeat.recorded_at)
    except ValueError:
        return True
    age = datetime.now(timezone.utc) - recorded.astimezone(timezone.utc)
    return age.total_seconds() > stale_after_seconds


def _tmux_pane_alive(target: str) -> bool:
    if not target:
        return False
    result = subprocess.run(
        ["tmux", "list-panes", "-t", target, "-F", "#{pane_dead} #{pane_current_command}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if parts and parts[0] == "1":
            return False
        if len(parts) >= 2 and parts[1] in ("bash", "zsh", "sh", "fish"):
            return False
    return True


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        Path(tmp_name).replace(path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def registry_snapshot(team_name: str) -> dict[str, dict[str, Any]]:
    store = WorkerRuntimeStore(team_name)
    snapshot: dict[str, dict[str, Any]] = {}
    for worker in store.list_workers():
        snapshot[worker.worker_name] = {
            "backend": worker.spawn_backend,
            "tmux_target": worker.tmux_target,
            "pid": worker.pid,
            "command": worker.command,
            "state": worker.state.value,
            "current_stage": worker.current_stage,
            "assignment": worker.assignment,
            "session_id": worker.session_id,
            "last_exit_reason": worker.last_exit_reason,
            "last_heartbeat": worker.last_heartbeat.recorded_at if worker.last_heartbeat else "",
            "workspace": worker.workspace.model_dump(by_alias=True) if worker.workspace else None,
        }
    return snapshot
