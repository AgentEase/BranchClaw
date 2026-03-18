"""Event models for the unified clawteam event stream."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventEnvelope(BaseModel):
    """Canonical envelope for all persisted team events."""

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    event_type: str
    team_name: str
    run_id: str = ""
    stage_id: str = ""
    worker_name: str = ""
    correlation_id: str = ""
    timestamp: str = Field(default_factory=_now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)


class EventTypes:
    """Known event type constants."""

    RUN_CREATED = "run.created"

    STAGE_STARTED = "stage.started"
    STAGE_COMPLETED = "stage.completed"
    STAGE_ROLLED_BACK = "stage.rolled_back"

    WORKER_SPAWNED = "worker.spawned"
    WORKER_HEARTBEAT = "worker.heartbeat"
    WORKER_EXITED = "worker.exited"

    WORKSPACE_CREATED = "workspace.created"
    WORKSPACE_CHECKPOINTED = "workspace.checkpointed"
    WORKSPACE_MERGED = "workspace.merged"
    WORKSPACE_CLEANED = "workspace.cleaned"

    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_UNBLOCKED = "task.unblocked"
    TASK_REASSIGNED = "task.reassigned"

    CONSTRAINT_APPLIED = "constraint.applied"

    HUMAN_REVIEW_REQUESTED = "human.review_requested"
    HUMAN_REVIEW_RECORDED = "human.review_recorded"

    MESSAGE_SENT = "message.sent"
    COST_RECORDED = "cost.recorded"
