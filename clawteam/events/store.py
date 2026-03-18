"""Filesystem-backed event persistence for clawteam."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from clawteam.events.models import EventEnvelope


def _get_data_dir() -> Path:
    custom = os.environ.get("CLAWTEAM_DATA_DIR")
    if not custom:
        from clawteam.config import load_config

        custom = load_config().data_dir or None
    path = Path(custom) if custom else Path.home() / ".clawteam"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _events_root(team_name: str, *, create: bool = True) -> Path:
    root = _get_data_dir() / "events" / team_name
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _event_path(team_name: str, event: EventEnvelope) -> Path:
    stamp = event.timestamp.replace(":", "-").replace("+", "p")
    return _events_root(team_name) / f"evt-{stamp}-{event.event_id[:12]}.json"


def _run_index_path(team_name: str, run_id: str, *, create: bool = True) -> Path:
    path = _get_data_dir() / "runs" / team_name / run_id / "event-index.json"
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


class EventStore:
    """Persists canonical team events under the configured data dir."""

    def __init__(self, team_name: str):
        self.team_name = team_name

    @staticmethod
    def default_run_id() -> str:
        return os.environ.get("CLAWTEAM_RUN_ID", "")

    @staticmethod
    def default_stage_id() -> str:
        return os.environ.get("CLAWTEAM_STAGE_ID", "")

    @staticmethod
    def default_worker_name() -> str:
        return os.environ.get("CLAWTEAM_AGENT_NAME", "")

    @staticmethod
    def default_correlation_id() -> str:
        return os.environ.get("CLAWTEAM_CORRELATION_ID", "")

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        run_id: str | None = None,
        stage_id: str | None = None,
        worker_name: str | None = None,
        correlation_id: str | None = None,
        timestamp: str | None = None,
        event_id: str | None = None,
    ) -> EventEnvelope:
        event = EventEnvelope(
            event_id=event_id or uuid.uuid4().hex,
            event_type=event_type,
            team_name=self.team_name,
            run_id=run_id if run_id is not None else self.default_run_id(),
            stage_id=stage_id if stage_id is not None else self.default_stage_id(),
            worker_name=worker_name if worker_name is not None else self.default_worker_name(),
            correlation_id=(
                correlation_id if correlation_id is not None else self.default_correlation_id()
            ),
            timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
            payload=payload or {},
        )
        path = _event_path(self.team_name, event)
        self._atomic_write(path, event.model_dump_json(indent=2))
        if event.run_id:
            self._append_run_index(event, path)
        return event

    def list_events(
        self,
        *,
        run_id: str = "",
        stage_id: str = "",
        worker_name: str = "",
        correlation_id: str = "",
        event_types: Iterable[str] | None = None,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[EventEnvelope]:
        allowed_types = set(event_types or [])
        events: list[EventEnvelope] = []
        for path in self._candidate_paths(run_id=run_id):
            event = self._read_event(path)
            if event is None:
                continue
            if run_id and event.run_id != run_id:
                continue
            if stage_id and event.stage_id != stage_id:
                continue
            if worker_name and event.worker_name != worker_name:
                continue
            if correlation_id and event.correlation_id != correlation_id:
                continue
            if allowed_types and event.event_type not in allowed_types:
                continue
            events.append(event)
        events.sort(key=lambda item: item.timestamp, reverse=newest_first)
        if limit is not None:
            return events[:limit]
        return events

    def _candidate_paths(self, *, run_id: str) -> list[Path]:
        if run_id:
            index_path = _run_index_path(self.team_name, run_id, create=False)
            if index_path.exists():
                try:
                    data = json.loads(index_path.read_text(encoding="utf-8"))
                    files = data.get("events", [])
                    return [self._resolve_index_path(index_path.parent, item["path"]) for item in files]
                except Exception:
                    pass
        root = _events_root(self.team_name, create=False)
        if not root.exists():
            return []
        return sorted(root.glob("evt-*.json"))

    def _append_run_index(self, event: EventEnvelope, path: Path) -> None:
        index_path = _run_index_path(self.team_name, event.run_id)
        data: dict[str, Any]
        if index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        else:
            data = {}
        events = data.setdefault("events", [])
        rel_path = os.path.relpath(path, index_path.parent)
        events.append({
            "event_id": event.event_id,
            "event_type": event.event_type,
            "timestamp": event.timestamp,
            "stage_id": event.stage_id,
            "worker_name": event.worker_name,
            "correlation_id": event.correlation_id,
            "path": rel_path,
        })
        data["team_name"] = self.team_name
        data["run_id"] = event.run_id
        self._atomic_write(index_path, json.dumps(data, indent=2, ensure_ascii=False))

    @staticmethod
    def _read_event(path: Path) -> EventEnvelope | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return EventEnvelope.model_validate(data)
        except Exception:
            return None

    @staticmethod
    def _resolve_index_path(base_dir: Path, rel_path: str) -> Path:
        return (base_dir / rel_path).resolve()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.stem}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(content)
            Path(tmp_name).replace(path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
