from __future__ import annotations

import json
from pathlib import Path

from clawteam.board.collector import BoardCollector
from clawteam.events import EventStore, EventTypes
from clawteam.team.mailbox import MailboxManager
from clawteam.team.manager import TeamManager
from clawteam.team.models import TaskStatus
from clawteam.team.tasks import TaskStore


def test_event_store_writes_team_files_and_run_index(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(tmp_path))

    store = EventStore("demo")
    event = store.emit(
        EventTypes.RUN_CREATED,
        run_id="run-1",
        stage_id="bootstrap",
        worker_name="leader",
        payload={"hello": "world"},
    )

    event_files = list((tmp_path / "events" / "demo").glob("evt-*.json"))
    assert len(event_files) == 1
    assert json.loads(event_files[0].read_text(encoding="utf-8"))["event_id"] == event.event_id

    index_path = tmp_path / "runs" / "demo" / "run-1" / "event-index.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["run_id"] == "run-1"
    assert index["events"][0]["event_type"] == EventTypes.RUN_CREATED


def test_mailbox_history_reads_from_unified_event_stream(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(tmp_path))

    mailbox = MailboxManager("demo")
    mailbox.send(from_agent="alice", to="bob", content="hello")

    history = mailbox.get_event_log()
    assert len(history) == 1
    assert history[0].content == "hello"

    events = EventStore("demo").list_events(event_types=[EventTypes.MESSAGE_SENT])
    assert len(events) == 1
    assert events[0].payload["message"]["from"] == "alice"


def test_task_store_emits_created_updated_and_unblocked_events(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(tmp_path))

    store = TaskStore("demo")
    blocker = store.create("blocker", owner="alice")
    blocked = store.create("blocked", owner="bob", blocked_by=[blocker.id])
    store.update(blocker.id, status=TaskStatus.completed, caller="alice")
    store.update(blocked.id, owner="carol", caller="leader")

    events = EventStore("demo").list_events(limit=20)
    event_types = [event.event_type for event in events]
    assert EventTypes.TASK_CREATED in event_types
    assert EventTypes.TASK_UPDATED in event_types
    assert EventTypes.TASK_UNBLOCKED in event_types
    assert EventTypes.TASK_REASSIGNED in event_types


def test_board_collector_event_stream_filters(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(tmp_path))
    TeamManager.create_team(name="demo", leader_name="leader", leader_id="lead-1")

    store = EventStore("demo")
    store.emit(EventTypes.WORKER_SPAWNED, run_id="run-1", stage_id="spawn", worker_name="alice")
    store.emit(EventTypes.WORKER_SPAWNED, run_id="run-1", stage_id="spawn", worker_name="bob")
    store.emit(EventTypes.WORKSPACE_CREATED, run_id="run-2", stage_id="ws", worker_name="alice")

    payload = BoardCollector().collect_event_stream(
        "demo",
        run_id="run-1",
        worker_name="alice",
    )

    assert payload["filters"]["run_id"] == "run-1"
    assert len(payload["events"]) == 1
    assert payload["events"][0]["worker_name"] == "alice"
