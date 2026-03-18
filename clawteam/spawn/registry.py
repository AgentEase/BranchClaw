"""Compatibility wrapper around the unified worker runtime store."""

from __future__ import annotations

from clawteam.worker_runtime.models import WorkerState
from clawteam.worker_runtime.store import WorkerRuntimeStore, registry_snapshot


def register_agent(
    team_name: str,
    agent_name: str,
    backend: str,
    tmux_target: str = "",
    pid: int = 0,
    command: list[str] | None = None,
) -> None:
    """Record spawn info for an agent in the unified worker store."""
    WorkerRuntimeStore(team_name).record_spawn(
        worker_name=agent_name,
        backend=backend,
        tmux_target=tmux_target,
        pid=pid,
        command=command or [],
        state=WorkerState.ready,
    )


def get_registry(team_name: str) -> dict[str, dict]:
    """Return worker runtime records in the legacy registry shape."""
    return registry_snapshot(team_name)


def is_agent_alive(team_name: str, agent_name: str) -> bool | None:
    """Check whether an agent is alive based on process or heartbeat data."""
    return WorkerRuntimeStore(team_name).is_worker_alive(agent_name)


def list_dead_agents(team_name: str) -> list[str]:
    """Return names of workers that appear dead or stale."""
    return WorkerRuntimeStore(team_name).list_dead_workers()
