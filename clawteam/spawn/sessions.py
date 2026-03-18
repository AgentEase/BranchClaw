"""Compatibility wrapper around worker runtime session persistence."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from clawteam.worker_runtime.models import now_iso
from clawteam.worker_runtime.store import WorkerRuntimeStore


class SessionState(BaseModel):
    """Persisted session state for an agent."""

    model_config = {"populate_by_name": True}

    agent_name: str = Field(alias="agentName")
    team_name: str = Field(alias="teamName")
    session_id: str = Field(default="", alias="sessionId")
    last_task_id: str = Field(default="", alias="lastTaskId")
    saved_at: str = Field(default_factory=now_iso, alias="savedAt")
    state: dict[str, Any] = Field(default_factory=dict)


class SessionStore:
    """Legacy session API backed by unified worker runtime records."""

    def __init__(self, team_name: str):
        self.team_name = team_name
        self.runtime = WorkerRuntimeStore(team_name)

    def save(
        self,
        agent_name: str,
        session_id: str = "",
        last_task_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> SessionState:
        record = self.runtime.save_session(
            worker_name=agent_name,
            session_id=session_id,
            last_task_id=last_task_id,
            state_payload=state or {},
        )
        return self._to_session(record)

    def load(self, agent_name: str) -> SessionState | None:
        record = self.runtime.load(agent_name)
        if record is None:
            return None
        if not record.session_id and not record.last_task_id and not record.metadata.get("session"):
            return None
        return self._to_session(record)

    def clear(self, agent_name: str) -> bool:
        return self.runtime.clear_session(agent_name)

    def list_sessions(self) -> list[SessionState]:
        sessions: list[SessionState] = []
        for record in self.runtime.list_workers():
            if record.session_id or record.last_task_id or record.metadata.get("session"):
                sessions.append(self._to_session(record))
        return sessions

    def _to_session(self, record) -> SessionState:
        return SessionState(
            agentName=record.worker_name,
            teamName=record.team_name,
            sessionId=record.session_id,
            lastTaskId=record.last_task_id,
            savedAt=record.updated_at,
            state=record.metadata.get("session", {}),
        )
