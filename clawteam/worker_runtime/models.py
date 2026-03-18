"""Unified worker runtime models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def now_iso() -> str:
    """Return current UTC timestamp as ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


class WorkerState(str, Enum):
    """Lifecycle state for a worker."""

    created = "created"
    ready = "ready"
    running = "running"
    idle = "idle"
    blocked = "blocked"
    exited = "exited"
    archived = "archived"


class WorkspaceBinding(BaseModel):
    """Git workspace binding for a worker."""

    branch: str = ""
    worktree_path: str = Field(default="", alias="worktreePath")
    repo_root: str = Field(default="", alias="repoRoot")
    base_branch: str = Field(default="", alias="baseBranch")
    bound_at: str = Field(default_factory=now_iso, alias="boundAt")


class Heartbeat(BaseModel):
    """Periodic runtime heartbeat emitted by worker CLI."""

    recorded_at: str = Field(default_factory=now_iso, alias="recordedAt")
    state: WorkerState | None = None
    assignment: str = ""
    current_stage: str = Field(default="", alias="currentStage")
    session_id: str = Field(default="", alias="sessionId")
    pid: int = 0
    tmux_target: str = Field(default="", alias="tmuxTarget")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionAttempt(BaseModel):
    """Single execution attempt for a worker process."""

    attempt: int = 1
    backend: str = ""
    pid: int = 0
    tmux_target: str = Field(default="", alias="tmuxTarget")
    command: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=now_iso, alias="startedAt")
    exited_at: str = Field(default="", alias="exitedAt")
    exit_reason: str = Field(default="", alias="exitReason")


class WorkerRecord(BaseModel):
    """Canonical runtime record persisted for each worker."""

    model_config = {"populate_by_name": True}

    team_name: str = Field(alias="teamName")
    worker_name: str = Field(alias="workerName")
    worker_id: str = Field(default="", alias="workerId")
    state: WorkerState = WorkerState.created
    created_at: str = Field(default_factory=now_iso, alias="createdAt")
    updated_at: str = Field(default_factory=now_iso, alias="updatedAt")
    archived_at: str = Field(default="", alias="archivedAt")

    spawn_backend: str = Field(default="", alias="spawnBackend")
    pid: int = 0
    tmux_target: str = Field(default="", alias="tmuxTarget")
    command: list[str] = Field(default_factory=list)

    workspace: WorkspaceBinding | None = None

    assignment: str = ""
    current_stage: str = Field(default="", alias="currentStage")
    session_id: str = Field(default="", alias="sessionId")
    last_task_id: str = Field(default="", alias="lastTaskId")
    last_exit_reason: str = Field(default="", alias="lastExitReason")
    last_heartbeat: Heartbeat | None = Field(default=None, alias="lastHeartbeat")

    execution_attempts: list[ExecutionAttempt] = Field(default_factory=list, alias="executionAttempts")
    metadata: dict[str, Any] = Field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = now_iso()
