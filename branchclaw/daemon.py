"""Global daemon process management for BranchClaw."""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from hashlib import sha1
from pathlib import Path
from typing import Any

from branchclaw.config import load_config
from branchclaw.mcp_state import ensure_mcp_server
from branchclaw.models import (
    DaemonStatus,
    DataDirRegistry,
    ManagedProcessRecord,
    new_id,
    now_iso,
)
from branchclaw.runtime import (
    launch_supervisor_process,
    pid_alive,
    read_worker_status,
    tmux_target_alive,
    worker_launch_path,
    worker_stop_path,
)
from branchclaw.storage import save_json

DAEMON_PROCESS_ENV = "BRANCHCLAW_DAEMON_PROCESS"
DAEMON_ROOT_ENV = "BRANCHCLAW_DAEMON_ROOT"


class BranchClawDaemonError(RuntimeError):
    """Raised when the BranchClaw daemon is unavailable or reports an error."""


def daemon_root() -> Path:
    custom = os.environ.get(DAEMON_ROOT_ENV)
    root = Path(custom).expanduser() if custom else Path.home() / ".branchclawd"
    root.mkdir(parents=True, exist_ok=True)
    return root


def daemon_socket_path() -> Path:
    return daemon_root() / "daemon.sock"


def daemon_state_path() -> Path:
    return daemon_root() / "state.json"


def board_status_path(data_dir: str | Path) -> Path:
    path = Path(data_dir) / "board" / "server.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _event_level(event_type: str, payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    explicit = str(payload.get("level") or payload.get("log_level") or "").lower().strip()
    if explicit in {"debug", "info", "warning", "error"}:
        return "info" if explicit == "debug" else explicit

    status = str(payload.get("status") or payload.get("result", {}).get("status") or "").lower()
    event_type = event_type.lower()

    if any(token in event_type for token in ("failed", "rejected", "blocked")):
        return "error"
    if "intervention_opened" in event_type:
        return "error"
    if any(token in status for token in ("failed", "blocked", "error")):
        return "error"

    warning_tokens = (
        "requested",
        "stale",
        "replan",
        "remediation_attempted",
        "remediation_failed",
        "superseded",
        "merge.blocked",
    )
    if any(token in event_type for token in warning_tokens):
        return "warning"
    if status in {"pending", "pending_approval", "stale"}:
        return "warning"
    return "info"


def in_daemon_process() -> bool:
    return os.environ.get(DAEMON_PROCESS_ENV, "") == "1"


@contextmanager
def data_dir_context(data_dir: str):
    previous = os.environ.get("BRANCHCLAW_DATA_DIR")
    os.environ["BRANCHCLAW_DATA_DIR"] = data_dir
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("BRANCHCLAW_DATA_DIR", None)
        else:
            os.environ["BRANCHCLAW_DATA_DIR"] = previous


def _default_status() -> DaemonStatus:
    return DaemonStatus(
        running=False,
        daemon_pid=0,
        socket_path=str(daemon_socket_path()),
    )


def _load_status() -> DaemonStatus:
    path = daemon_state_path()
    if not path.exists():
        return _default_status()
    try:
        return DaemonStatus.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return _default_status()


def read_saved_daemon_status() -> DaemonStatus:
    """Read the last persisted daemon status without contacting the live daemon."""
    return _load_status()


def _save_status(status: DaemonStatus) -> None:
    status.socket_path = str(daemon_socket_path())
    save_json(daemon_state_path(), json.loads(status.model_dump_json()))


def _rebuild_data_dirs(
    processes: list[ManagedProcessRecord],
    known_data_dirs: set[str] | None = None,
) -> list[DataDirRegistry]:
    grouped: dict[str, list[str]] = {}
    timestamp = now_iso()
    for record in processes:
        grouped.setdefault(record.data_dir, []).append(record.id)
    for data_dir in known_data_dirs or set():
        grouped.setdefault(data_dir, [])
    return [
        DataDirRegistry(data_dir=data_dir, process_ids=sorted(ids), last_seen_at=timestamp)
        for data_dir, ids in sorted(grouped.items())
    ]


def _remove_socket_if_stale() -> None:
    socket_path = daemon_socket_path()
    if socket_path.exists():
        socket_path.unlink(missing_ok=True)


def _pid_matches_branchclaw_daemon(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    if not raw:
        return False
    command = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
    return "branchclaw" in command and "daemon" in command and "serve" in command


def _clear_saved_daemon_status(status: DaemonStatus) -> None:
    historical_dirs = {item.data_dir for item in status.data_dirs}
    status.running = False
    status.daemon_pid = 0
    status.started_at = ""
    status.processes = []
    status.data_dirs = _rebuild_data_dirs([], historical_dirs)
    status.dashboard_running = False
    status.dashboard_host = ""
    status.dashboard_port = 0
    status.dashboard_url = ""
    _save_status(status)


def stop_orphaned_daemon_process(*, timeout_seconds: float = 3.0) -> dict[str, Any]:
    status = _load_status()
    pid = status.daemon_pid
    if pid <= 0 or not status.running:
        _remove_socket_if_stale()
        _clear_saved_daemon_status(status)
        return {"stopped": False, "reason": "not_running"}

    if not _pid_matches_branchclaw_daemon(pid) or not pid_alive(pid):
        _remove_socket_if_stale()
        _clear_saved_daemon_status(status)
        return {"stopped": True, "reason": "stale_state_cleaned", "daemon_pid": pid}

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_socket_if_stale()
        _clear_saved_daemon_status(status)
        return {"stopped": True, "reason": "stale_state_cleaned", "daemon_pid": pid}

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not pid_alive(pid):
            _remove_socket_if_stale()
            _clear_saved_daemon_status(status)
            return {"stopped": True, "reason": "orphaned_daemon_terminated", "daemon_pid": pid}
        time.sleep(0.1)

    os.kill(pid, signal.SIGKILL)
    hard_deadline = time.time() + 1.0
    while time.time() < hard_deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.05)
    _remove_socket_if_stale()
    _clear_saved_daemon_status(status)
    return {"stopped": True, "reason": "orphaned_daemon_killed", "daemon_pid": pid}


def _data_dir_key(data_dir: str) -> str:
    return sha1(str(Path(data_dir).resolve()).encode("utf-8")).hexdigest()[:12]


def _looks_like_data_dir(path: Path) -> bool:
    try:
        return path.is_dir() and path.name == ".branchclaw" and (path / "runs").is_dir()
    except OSError:
        return False


def _discover_historical_data_dirs() -> set[str]:
    discovered: set[str] = set()
    configured = load_config().data_dir
    if configured:
        configured_path = Path(configured).expanduser().resolve()
        if _looks_like_data_dir(configured_path):
            discovered.add(str(configured_path))

    home_default = Path.home() / ".branchclaw"
    if _looks_like_data_dir(home_default):
        discovered.add(str(home_default.resolve()))

    cwd = Path.cwd().resolve()
    cwd_default = cwd / ".branchclaw"
    if _looks_like_data_dir(cwd_default):
        discovered.add(str(cwd_default))

    artifacts_root = cwd / "artifacts"
    if artifacts_root.exists():
        for candidate in artifacts_root.rglob(".branchclaw"):
            if _looks_like_data_dir(candidate):
                discovered.add(str(candidate.resolve()))

    return discovered


def _request_daemon(payload: dict[str, Any], *, timeout_seconds: float = 10.0) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_seconds)
    try:
        sock.connect(str(daemon_socket_path()))
        sock.sendall(json.dumps(payload).encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise BranchClawDaemonError(
            "BranchClaw daemon is not running. Start it with `branchclaw daemon start`."
        ) from exc
    finally:
        sock.close()
    if not chunks:
        raise BranchClawDaemonError("BranchClaw daemon returned an empty response")
    response = json.loads(b"".join(chunks).decode("utf-8"))
    if not response.get("ok", False):
        raise BranchClawDaemonError(response.get("error", "BranchClaw daemon request failed"))
    result = response.get("result", {})
    return result if isinstance(result, dict) else {}


class BranchClawDaemonClient:
    """Thin client for the global BranchClaw daemon."""

    @classmethod
    def is_running(cls) -> bool:
        try:
            response = _request_daemon({"action": "ping"}, timeout_seconds=1.0)
        except BranchClawDaemonError:
            return False
        return bool(response.get("running"))

    @classmethod
    def require_running(cls) -> BranchClawDaemonClient:
        if not cls.is_running():
            raise BranchClawDaemonError(
                "BranchClaw daemon is not running. Start it with `branchclaw daemon start`."
            )
        return cls()

    @classmethod
    def optional(cls) -> BranchClawDaemonClient | None:
        return cls() if cls.is_running() else None

    def status(self) -> dict[str, Any]:
        return _request_daemon({"action": "status"})

    def ps(self) -> dict[str, Any]:
        return _request_daemon({"action": "ps"})

    def stop(self) -> dict[str, Any]:
        return _request_daemon({"action": "shutdown"})

    def stop_service(self, process_id: str) -> dict[str, Any]:
        return _request_daemon({"action": "stop_service", "process_id": process_id})

    def ensure_mcp_server(self, *, data_dir: str, run_id: str = "") -> dict[str, Any]:
        return _request_daemon(
            {
                "action": "ensure_mcp_server",
                "data_dir": str(Path(data_dir).resolve()),
                "run_id": run_id,
            }
        )

    def stop_mcp_server(self, *, data_dir: str) -> dict[str, Any]:
        return _request_daemon(
            {
                "action": "stop_mcp_server",
                "data_dir": str(Path(data_dir).resolve()),
            }
        )

    def launch_supervisor(self, *, data_dir: str, run_id: str, worker_name: str) -> dict[str, Any]:
        return _request_daemon(
            {
                "action": "launch_supervisor",
                "data_dir": str(Path(data_dir).resolve()),
                "run_id": run_id,
                "worker_name": worker_name,
            }
        )

    def stop_worker(self, *, data_dir: str, run_id: str, worker_name: str) -> dict[str, Any]:
        return _request_daemon(
            {
                "action": "stop_worker",
                "data_dir": str(Path(data_dir).resolve()),
                "run_id": run_id,
                "worker_name": worker_name,
            }
        )

    def reconcile_run(
        self,
        *,
        data_dir: str,
        run_id: str,
        worker_names: list[str] | None = None,
    ) -> dict[str, Any]:
        return _request_daemon(
            {
                "action": "reconcile_run",
                "data_dir": str(Path(data_dir).resolve()),
                "run_id": run_id,
                "worker_names": list(worker_names or []),
            }
        )


def start_daemon_process(
    *,
    timeout_seconds: float = 10.0,
    host: str | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    client = BranchClawDaemonClient.optional()
    if client is not None:
        return client.status()

    stop_orphaned_daemon_process(timeout_seconds=2.0)
    _remove_socket_if_stale()
    env = {**os.environ, DAEMON_PROCESS_ENV: "1"}
    env.pop("BRANCHCLAW_DATA_DIR", None)
    command = [
        sys.executable,
        "-m",
        "branchclaw",
        "daemon",
        "serve",
        "--socket",
        str(daemon_socket_path()),
    ]
    if host:
        command.extend(["--host", host])
    if port is not None:
        command.extend(["--port", str(port)])
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            break
        client = BranchClawDaemonClient.optional()
        if client is not None:
            return client.status()
        time.sleep(0.1)
    raise BranchClawDaemonError("Timed out waiting for BranchClaw daemon to start")


def _pid_for_record(record: ManagedProcessRecord) -> int:
    if record.process_kind == "supervisor":
        return record.supervisor_pid or record.pid
    return record.pid


def _record_alive(record: ManagedProcessRecord) -> bool:
    pid = _pid_for_record(record)
    if record.process_kind == "supervisor":
        return pid_alive(pid)
    return pid_alive(pid)


def _read_board_status(data_dir: str) -> dict[str, Any]:
    path = board_status_path(data_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_board_status(data_dir: str, payload: dict[str, Any]) -> None:
    save_json(board_status_path(data_dir), payload)


def _adopt_worker_processes(data_dir: str) -> list[ManagedProcessRecord]:
    root = Path(data_dir) / "runs"
    adopted: list[ManagedProcessRecord] = []
    if not root.exists():
        return adopted
    for status_path in sorted(root.glob("*/runtime/*/status.json")):
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        run_id = status_path.parts[-4]
        worker_name = status_path.parent.name
        supervisor_pid = int(payload.get("supervisor_pid") or 0)
        child_pid = int(payload.get("child_pid") or 0)
        tmux_target = str(payload.get("tmux_target") or "")
        alive = pid_alive(supervisor_pid)
        if not alive and child_pid:
            alive = pid_alive(child_pid)
        if not alive and tmux_target:
            alive = tmux_target_alive(tmux_target)
        if not alive:
            continue
        adopted.append(
            ManagedProcessRecord(
                id=new_id("proc-"),
                data_dir=str(Path(data_dir).resolve()),
                process_kind="supervisor",
                process_key=f"{run_id}:{worker_name}",
                pid=supervisor_pid,
                child_pid=child_pid,
                supervisor_pid=supervisor_pid,
                run_id=run_id,
                worker_name=worker_name,
                status="running",
                metadata={"tmux_target": tmux_target},
            )
        )
    return adopted


def _adopt_mcp_process(data_dir: str) -> ManagedProcessRecord | None:
    path = Path(data_dir) / "mcp" / "server.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    pid = int(payload.get("pid") or 0)
    if not pid_alive(pid):
        return None
    return ManagedProcessRecord(
        id=new_id("proc-"),
        data_dir=str(Path(data_dir).resolve()),
        process_kind="mcp",
        process_key=str(Path(data_dir).resolve()),
        pid=pid,
        host=str(payload.get("host", "")),
        port=int(payload.get("port") or 0),
        status="running",
    )


def _adopt_processes_for_data_dir(data_dir: str, current: list[ManagedProcessRecord]) -> list[ManagedProcessRecord]:
    resolved = str(Path(data_dir).resolve())
    existing_keys = {(record.process_kind, record.process_key) for record in current if record.data_dir == resolved}
    adopted: list[ManagedProcessRecord] = []
    mcp_record = _adopt_mcp_process(resolved)
    if mcp_record and (mcp_record.process_kind, mcp_record.process_key) not in existing_keys:
        adopted.append(mcp_record)
    board_status_path(resolved).unlink(missing_ok=True)
    for record in _adopt_worker_processes(resolved):
        if (record.process_kind, record.process_key) not in existing_keys:
            adopted.append(record)
    return adopted


class BranchClawDaemonController:
    def __init__(
        self,
        *,
        dashboard_host: str = "",
        dashboard_port: int = 0,
        dashboard_poll_interval: float = 2.0,
    ) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._status = _load_status()
        self._dashboard_server = None
        self._dashboard_thread: threading.Thread | None = None
        self._status.running = True
        self._status.daemon_pid = os.getpid()
        self._status.socket_path = str(daemon_socket_path())
        self._status.started_at = now_iso()
        self._start_dashboard_server(
            host=dashboard_host or load_config().board_host,
            port=dashboard_port if dashboard_port > 0 else load_config().board_port,
            poll_interval=dashboard_poll_interval,
        )
        self._refresh_state()
        _save_status(self._status)
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _remember_data_dir(self, data_dir: str) -> None:
        resolved = str(Path(data_dir).resolve())
        if any(item.data_dir == resolved for item in self._status.data_dirs):
            return
        self._status.data_dirs.append(DataDirRegistry(data_dir=resolved))

    def _refresh_state(self) -> None:
        alive: list[ManagedProcessRecord] = []
        for record in self._status.processes:
            if _record_alive(record):
                record.last_seen_at = now_iso()
                if record.process_kind == "supervisor":
                    status = read_worker_status(record.run_id, record.worker_name)
                    record.child_pid = int(status.get("child_pid") or record.child_pid or 0)
                    record.supervisor_pid = int(status.get("supervisor_pid") or record.supervisor_pid or record.pid)
                    record.status = str(status.get("status") or record.status)
                    tmux_target = status.get("tmux_target")
                    if tmux_target:
                        record.metadata["tmux_target"] = tmux_target
                alive.append(record)
        known_dirs = {item.data_dir for item in self._status.data_dirs}
        known_dirs.update(record.data_dir for record in alive)
        known_dirs.update(_discover_historical_data_dirs())
        for data_dir in sorted(known_dirs):
            alive.extend(_adopt_processes_for_data_dir(data_dir, alive))
        self._status.processes = alive
        self._status.data_dirs = _rebuild_data_dirs(alive, known_dirs)
        self._status.daemon_pid = os.getpid()
        self._status.running = True
        self._status.socket_path = str(daemon_socket_path())
        _save_status(self._status)

    def _managed_record(self, *, process_kind: str, data_dir: str) -> ManagedProcessRecord | None:
        resolved = str(Path(data_dir).resolve())
        for record in self._status.processes:
            if record.process_kind == process_kind and record.data_dir == resolved:
                return record
        return None

    def _terminate_record(self, record: ManagedProcessRecord) -> None:
        if record.process_kind == "supervisor" and record.run_id and record.worker_name:
            with data_dir_context(record.data_dir):
                stop_path = worker_stop_path(record.run_id, record.worker_name)
                stop_path.parent.mkdir(parents=True, exist_ok=True)
                stop_path.touch(exist_ok=True)
        pid = _pid_for_record(record)
        if pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(0.2)
        if pid > 0 and pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if record.process_kind == "mcp":
            Path(record.data_dir, "mcp", "server.json").unlink(missing_ok=True)

    def _watchdog_loop(self) -> None:
        while not self._stop_event.wait(max(0.5, float(load_config().daemon_watchdog_interval))):
            try:
                with self._lock:
                    self._refresh_state()
                    managed_data_dirs = sorted({item.data_dir for item in self._status.data_dirs})
                for data_dir in managed_data_dirs:
                    with data_dir_context(data_dir):
                        from branchclaw.service import BranchClawService

                        service = BranchClawService()
                        for projection in service.list_runs():
                            run_id = projection.run.id
                            service._reconcile_workers_local(run_id)
                            service._apply_worker_watchdog_policies_local(run_id)
                            service.dispatch_feature_backlog(run_id)
                with self._lock:
                    self._refresh_state()
            except Exception:
                # Watchdog errors must not kill the daemon; operator surfaces still expose the last failure state.
                continue

    def _start_dashboard_server(self, *, host: str, port: int, poll_interval: float) -> None:
        from branchclaw.board import DaemonDashboardBackend, build_server

        backend = DaemonDashboardBackend(self)
        server = build_server(
            host=host,
            port=port,
            poll_interval=poll_interval,
            backend=backend,
        )
        self._dashboard_server = server
        actual_host, actual_port = server.server_address
        self._status.dashboard_running = True
        self._status.dashboard_host = str(actual_host)
        self._status.dashboard_port = int(actual_port)
        self._status.dashboard_url = f"http://{actual_host}:{actual_port}"
        self._dashboard_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
        )
        self._dashboard_thread.start()

    def _stop_dashboard_server(self) -> None:
        if self._dashboard_server is not None:
            self._dashboard_server.shutdown()
            self._dashboard_server.server_close()
            self._dashboard_server = None
        if self._dashboard_thread is not None:
            self._dashboard_thread.join(timeout=2)
            self._dashboard_thread = None
        self._status.dashboard_running = False
        self._status.dashboard_host = ""
        self._status.dashboard_port = 0
        self._status.dashboard_url = ""

    def _resolve_data_dir_key(self, data_dir_key: str) -> str:
        for item in self._status.data_dirs:
            if _data_dir_key(item.data_dir) == data_dir_key:
                return item.data_dir
        raise BranchClawDaemonError(f"Unknown data dir key '{data_dir_key}'")

    def _resolve_or_attach_data_dir(
        self,
        *,
        data_dir_key: str = "",
        data_dir: str = "",
    ) -> tuple[str, str]:
        if data_dir_key:
            resolved = self._resolve_data_dir_key(data_dir_key)
            return resolved, _data_dir_key(resolved)
        if data_dir:
            path = Path(data_dir).expanduser()
            if not path.is_absolute():
                raise BranchClawDaemonError("Dashboard data dir must be an absolute path")
            if path.name != ".branchclaw":
                raise BranchClawDaemonError("Dashboard data dir must point to a .branchclaw directory")
            path.mkdir(parents=True, exist_ok=True)
            resolved = str(path.resolve())
            self._remember_data_dir(resolved)
            self._status.data_dirs = _rebuild_data_dirs(
                self._status.processes,
                {item.data_dir for item in self._status.data_dirs} | {resolved},
            )
            _save_status(self._status)
            return resolved, _data_dir_key(resolved)
        if self._status.data_dirs:
            resolved = self._status.data_dirs[0].data_dir
            return resolved, _data_dir_key(resolved)
        raise BranchClawDaemonError("No dashboard data dir is selected")

    def dashboard_status_payload(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_state()
            return self._status.model_dump(mode="json")

    def dashboard_processes_payload(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_state()
            return [item.model_dump(mode="json") for item in self._status.processes]

    def dashboard_runs_payload(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_state()
            data_dirs = [item.data_dir for item in self._status.data_dirs]
            processes = [item.model_dump(mode="json") for item in self._status.processes]

        from branchclaw.board import summarize_runs

        runs: list[dict[str, Any]] = []
        for data_dir in data_dirs:
            key = _data_dir_key(data_dir)
            with data_dir_context(data_dir):
                current_runs = summarize_runs()
            related = [item for item in processes if item["data_dir"] == data_dir]
            for run in current_runs:
                run["dataDirKey"] = key
                run["ownerDataDir"] = data_dir
                run_processes = [item for item in related if item.get("run_id") == run["id"]]
                run["managedProcessCount"] = len(run_processes)
                run["managedProcessKinds"] = sorted({item["process_kind"] for item in run_processes})
                runs.append(run)

        runs.sort(key=lambda item: item.get("lastEventAt") or item.get("createdAt") or "", reverse=True)
        return runs

    def dashboard_data_dirs_payload(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_state()
            registries = [item.model_dump(mode="json") for item in self._status.data_dirs]
            processes = [item.model_dump(mode="json") for item in self._status.processes]

        runs = self.dashboard_runs_payload()
        payload: list[dict[str, Any]] = []
        for registry in registries:
            data_dir = registry["data_dir"]
            key = _data_dir_key(data_dir)
            scoped_runs = [item for item in runs if item["dataDirKey"] == key]
            scoped_processes = [item for item in processes if item["data_dir"] == data_dir]
            payload.append(
                {
                    "dataDirKey": key,
                    "dataDir": data_dir,
                    "lastSeenAt": registry.get("last_seen_at", ""),
                    "processCount": len(scoped_processes),
                    "runCount": len(scoped_runs),
                    "liveWorkers": sum(int(item.get("liveWorkers", 0)) for item in scoped_runs),
                    "unhealthyWorkers": sum(int(item.get("unhealthyWorkers", 0)) for item in scoped_runs),
                    "pendingApprovals": sum(int(item.get("pendingApprovals", 0)) for item in scoped_runs),
                }
            )
        return payload

    def dashboard_run_payload(self, data_dir_key: str, run_id: str) -> dict[str, Any]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.board import summarize_run

            payload = summarize_run(run_id)
        payload["run"]["dataDirKey"] = data_dir_key
        payload["run"]["ownerDataDir"] = data_dir
        related = [
            item.model_dump(mode="json")
            for item in self._status.processes
            if item.data_dir == data_dir and item.run_id == run_id
        ]
        payload["run"]["managedProcessCount"] = len(related)
        payload["run"]["managedProcessKinds"] = sorted({item["process_kind"] for item in related})
        return payload

    def dashboard_recent_events_payload(
        self,
        data_dir_key: str,
        run_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.storage import EventStore

            events = EventStore().list_events(run_id)
        filtered = [item for item in events if item.event_type != "worker.heartbeat"]
        rows: list[dict[str, Any]] = []
        for item in filtered[-max(1, limit):]:
            payload = item.model_dump(mode="json")
            payload["level"] = _event_level(item.event_type, item.payload)
            rows.append(payload)
        return rows

    def dashboard_stop_worker(self, data_dir_key: str, run_id: str, worker_name: str) -> dict[str, Any]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.service import TERMINAL_WORKER_STATUSES, BranchClawService

            service = BranchClawService()
            projection = service.get_run(run_id, rebuild=True)
            worker = projection.workers.get(worker_name)
            if worker is None:
                raise BranchClawDaemonError(f"Worker '{worker_name}' not found in run '{run_id}'")
            service._request_worker_shutdown(worker)
            deadline = time.time() + 4.0
            while time.time() < deadline:
                current = service.get_run(run_id, rebuild=True).workers.get(worker_name)
                if current is not None and current.status in TERMINAL_WORKER_STATUSES:
                    worker = current
                    break
                service._reconcile_workers_local(run_id, worker_names=[worker_name])
                time.sleep(0.1)

            current = service.get_run(run_id, rebuild=True).workers.get(worker_name)
            if current is not None and current.status not in TERMINAL_WORKER_STATUSES:
                service._force_worker_termination(current)
                projection = service._reconcile_workers_local(run_id, worker_names=[worker_name])
                worker = projection.workers.get(worker_name, current)
            else:
                worker = current or worker
        return {
            "requested": True,
            "runId": run_id,
            "workerName": worker_name,
            "status": worker.status.value,
        }

    def dashboard_restart_worker(self, data_dir_key: str, run_id: str, worker_name: str) -> dict[str, Any]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.service import BranchClawService

            worker = BranchClawService().restart_worker(run_id, worker_name)
        with self._lock:
            self._refresh_state()
        return {
            "requested": True,
            "runId": run_id,
            "workerName": worker_name,
            "status": worker.status.value,
        }

    def dashboard_reconcile_run(
        self,
        data_dir_key: str,
        run_id: str,
        worker_names: list[str] | None = None,
    ) -> dict[str, Any]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.service import BranchClawService

            service = BranchClawService()
            projection = service.reconcile_workers(run_id, worker_names=worker_names or None)
        with self._lock:
            self._refresh_state()
        return {
            "runId": run_id,
            "ownerDataDir": data_dir,
            "workers": len(projection.workers),
            "pendingRecovery": projection.stats.pending_recovery_count,
        }

    def dashboard_approve_gate(
        self,
        data_dir_key: str,
        run_id: str,
        gate_id: str,
        *,
        actor: str = "",
        feedback: str = "",
    ) -> dict[str, Any]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.service import BranchClawService

            projection = BranchClawService().approve_gate(run_id, gate_id, actor=actor, feedback=feedback)
        with self._lock:
            self._refresh_state()
        return {
            "approved": True,
            "gateId": gate_id,
            "runStatus": projection.run.status.value,
        }

    def dashboard_reject_gate(
        self,
        data_dir_key: str,
        run_id: str,
        gate_id: str,
        *,
        actor: str = "",
        feedback: str = "",
    ) -> dict[str, Any]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.service import BranchClawService

            projection = BranchClawService().reject_gate(run_id, gate_id, actor=actor, feedback=feedback)
        with self._lock:
            self._refresh_state()
        return {
            "rejected": True,
            "gateId": gate_id,
            "runStatus": projection.run.status.value,
        }

    def dashboard_create_archive(
        self,
        data_dir_key: str,
        run_id: str,
        *,
        label: str = "",
        summary: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.service import BranchClawService

            archive, gate = BranchClawService().create_archive(run_id, label=label, summary=summary, actor=actor)
        with self._lock:
            self._refresh_state()
        return {
            "archiveId": archive.id,
            "gateId": gate.id,
            "status": archive.status.value,
        }

    def dashboard_request_restore(
        self,
        data_dir_key: str,
        run_id: str,
        archive_id: str,
        *,
        actor: str = "",
    ) -> dict[str, Any]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.service import BranchClawService

            gate = BranchClawService().request_restore(run_id, archive_id, actor=actor)
        with self._lock:
            self._refresh_state()
        return {
            "gateId": gate.id,
            "archiveId": archive_id,
            "status": gate.status.value,
        }

    def dashboard_request_merge(
        self,
        data_dir_key: str,
        run_id: str,
        *,
        archive_id: str,
        batch_id: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.service import BranchClawService

            gate = BranchClawService().request_merge(
                run_id,
                archive_id=archive_id,
                batch_id=batch_id,
                actor=actor,
            )
        with self._lock:
            self._refresh_state()
        return {
            "gateId": gate.id,
            "archiveId": archive_id,
            "batchId": batch_id,
            "status": gate.status.value,
        }

    def dashboard_request_promote(
        self,
        data_dir_key: str,
        run_id: str,
        *,
        batch_id: str,
        actor: str = "",
    ) -> dict[str, Any]:
        data_dir = self._resolve_data_dir_key(data_dir_key)
        with data_dir_context(data_dir):
            from branchclaw.service import BranchClawService

            gate = BranchClawService().request_promote(run_id, batch_id=batch_id, actor=actor)
        with self._lock:
            self._refresh_state()
        return {
            "gateId": gate.id,
            "batchId": batch_id,
            "status": gate.status.value,
        }

    def dashboard_create_run(
        self,
        *,
        data_dir_key: str = "",
        data_dir: str = "",
        repo: str,
        name: str,
        description: str = "",
        project_profile: str = "backend",
        spec_content: str = "",
        rules_content: str = "",
        direction: str = "",
        integration_ref: str = "",
        max_active_features: int = 2,
        initial_plan: str = "",
        author: str = "",
    ) -> dict[str, Any]:
        if not repo.strip():
            raise BranchClawDaemonError("Dashboard run creation requires a local repo path")
        if not name.strip():
            raise BranchClawDaemonError("Dashboard run creation requires a run name")
        if not initial_plan.strip():
            raise BranchClawDaemonError("Dashboard run creation requires an initial plan")
        with self._lock:
            resolved_data_dir, resolved_key = self._resolve_or_attach_data_dir(
                data_dir_key=data_dir_key,
                data_dir=data_dir,
            )
        with data_dir_context(resolved_data_dir):
            from branchclaw.service import BranchClawService

            service = BranchClawService()
            projection = service.create_run(
                name,
                description=description,
                project_profile=project_profile,
                spec_content=spec_content,
                rules_content=rules_content,
                repo=repo,
                direction=direction,
                integration_ref=integration_ref,
                max_active_features=max_active_features,
            )
            plan, gate = service.propose_plan(
                projection.run.id,
                initial_plan,
                summary="Initial dashboard plan",
                author=author,
            )
            refreshed = service.get_run(projection.run.id, rebuild=True)
        with self._lock:
            self._refresh_state()
        return {
            "dataDirKey": resolved_key,
            "ownerDataDir": resolved_data_dir,
            "runId": projection.run.id,
            "planId": plan.id,
            "gateId": gate.id,
            "runStatus": refreshed.run.status.value,
        }

    def dashboard_spawn_worker(
        self,
        data_dir_key: str,
        run_id: str,
        *,
        worker_name: str,
        task: str = "",
        backend: str = "tmux",
        command: str | list[str] | None = None,
        feature_id: str = "",
        skip_permissions: bool | None = None,
    ) -> dict[str, Any]:
        if not worker_name.strip():
            raise BranchClawDaemonError("Dashboard workspace creation requires a worker name")
        if not task.strip():
            raise BranchClawDaemonError("Dashboard workspace creation requires a task")
        data_dir = self._resolve_data_dir_key(data_dir_key)
        if isinstance(command, list):
            final_command = [str(item) for item in command if str(item).strip()]
        else:
            final_command = shlex.split(str(command or "").strip())
        if not final_command:
            final_command = ["claude"]
        with data_dir_context(data_dir):
            from branchclaw.service import BranchClawService

            worker = BranchClawService().spawn_worker(
                run_id,
                worker_name,
                command=final_command,
                backend=backend or "tmux",
                task=task,
                feature_id=feature_id,
                skip_permissions=skip_permissions,
            )
        with self._lock:
            self._refresh_state()
        return {
            "requested": True,
            "dataDirKey": data_dir_key,
            "runId": run_id,
            "workerName": worker.worker_name,
            "status": worker.status.value,
            "workspacePath": worker.workspace_path,
        }

    def dispatch(self, request: dict[str, Any], server: BranchClawUnixServer) -> dict[str, Any]:
        action = str(request.get("action", ""))
        with self._lock:
            self._refresh_state()
            if action == "ping":
                return self._status.model_dump(mode="json")
            if action == "status":
                return self._status.model_dump(mode="json")
            if action == "ps":
                return self._status.model_dump(mode="json")
            if action == "shutdown":
                self._stop_event.set()
                affected_runs = sorted(
                    {
                        (record.data_dir, record.run_id)
                        for record in self._status.processes
                        if record.process_kind == "supervisor" and record.run_id
                    }
                )
                for record in sorted(self._status.processes, key=lambda item: item.process_kind == "supervisor", reverse=True):
                    self._terminate_record(record)
                for data_dir_item, run_id in affected_runs:
                    with data_dir_context(data_dir_item):
                        from branchclaw.service import BranchClawService

                        BranchClawService()._reconcile_workers_local(run_id)
                self._stop_dashboard_server()
                historical_dirs = {item.data_dir for item in self._status.data_dirs}
                self._status.processes = []
                self._status.data_dirs = _rebuild_data_dirs([], historical_dirs)
                self._status.running = False
                _save_status(self._status)
                threading.Thread(target=server.shutdown, daemon=True).start()
                return {"stopped": True, "daemon_pid": os.getpid()}

            data_dir = str(request.get("data_dir") or "")
            if not data_dir:
                raise BranchClawDaemonError("Missing data_dir in daemon request")
            data_dir = str(Path(data_dir).resolve())
            self._remember_data_dir(data_dir)

            if action == "ensure_mcp_server":
                with data_dir_context(data_dir):
                    server_status = ensure_mcp_server(run_id=str(request.get("run_id", "")))
                existing = self._managed_record(process_kind="mcp", data_dir=data_dir)
                record = ManagedProcessRecord(
                    id=existing.id if existing else new_id("proc-"),
                    data_dir=data_dir,
                    process_kind="mcp",
                    process_key=data_dir,
                    pid=int(server_status.get("pid", 0)),
                    host=str(server_status.get("host", "")),
                    port=int(server_status.get("port", 0)),
                    status="running",
                )
                self._status.processes = [
                    item for item in self._status.processes if not (item.process_kind == "mcp" and item.data_dir == data_dir)
                ] + [record]
                self._status.data_dirs = _rebuild_data_dirs(
                    self._status.processes,
                    {item.data_dir for item in self._status.data_dirs} | {data_dir},
                )
                _save_status(self._status)
                return {
                    **server_status,
                    "processId": record.id,
                    "ownerDataDir": data_dir,
                    "managedStatus": record.status,
                    "daemonPid": os.getpid(),
                }

            if action == "stop_mcp_server":
                record = self._managed_record(process_kind="mcp", data_dir=data_dir)
                if record is None:
                    return {"stopped": False, "reason": "not_found", "ownerDataDir": data_dir}
                self._terminate_record(record)
                self._status.processes = [item for item in self._status.processes if item.id != record.id]
                self._status.data_dirs = _rebuild_data_dirs(
                    self._status.processes,
                    {item.data_dir for item in self._status.data_dirs} | {data_dir},
                )
                _save_status(self._status)
                return {"stopped": True, "processId": record.id, "ownerDataDir": data_dir}

            if action == "launch_supervisor":
                run_id = str(request.get("run_id", ""))
                worker_name = str(request.get("worker_name", ""))
                if not run_id or not worker_name:
                    raise BranchClawDaemonError("Missing run_id or worker_name for supervisor launch")
                with data_dir_context(data_dir):
                    payload_path = worker_launch_path(run_id, worker_name)
                    if not payload_path.exists():
                        raise BranchClawDaemonError(f"Missing worker launch payload for '{worker_name}'")
                    try:
                        payload = json.loads(payload_path.read_text(encoding="utf-8"))
                    except Exception as exc:
                        raise BranchClawDaemonError(f"Invalid worker launch payload for '{worker_name}'") from exc
                    payload["managed_by_daemon"] = True
                    payload["daemon_pid"] = os.getpid()
                    save_json(payload_path, payload)
                    process = launch_supervisor_process(run_id, worker_name)
                record = ManagedProcessRecord(
                    id=new_id("proc-"),
                    data_dir=data_dir,
                    process_kind="supervisor",
                    process_key=f"{run_id}:{worker_name}",
                    pid=process.pid,
                    supervisor_pid=process.pid,
                    run_id=run_id,
                    worker_name=worker_name,
                    status="starting",
                )
                self._status.processes = [
                    item
                    for item in self._status.processes
                    if not (
                        item.process_kind == "supervisor"
                        and item.data_dir == data_dir
                        and item.process_key == record.process_key
                    )
                ] + [record]
                self._status.data_dirs = _rebuild_data_dirs(
                    self._status.processes,
                    {item.data_dir for item in self._status.data_dirs} | {data_dir},
                )
                _save_status(self._status)
                return {
                    "processId": record.id,
                    "supervisorPid": process.pid,
                    "ownerDataDir": data_dir,
                    "managedStatus": record.status,
                    "daemonPid": os.getpid(),
                }

            if action == "stop_worker":
                run_id = str(request.get("run_id", ""))
                worker_name = str(request.get("worker_name", ""))
                with data_dir_context(data_dir):
                    from branchclaw.service import BranchClawService

                    service = BranchClawService()
                    projection = service.get_run(run_id, rebuild=True)
                    worker = projection.workers.get(worker_name)
                    if worker is None:
                        raise BranchClawDaemonError(f"Worker '{worker_name}' not found in run '{run_id}'")
                    service._request_worker_shutdown(worker)
                return {
                    "runId": run_id,
                    "workerName": worker_name,
                    "ownerDataDir": data_dir,
                    "requested": True,
                }

            if action == "reconcile_run":
                run_id = str(request.get("run_id", ""))
                worker_names = [str(item) for item in request.get("worker_names", []) if str(item)]
                with data_dir_context(data_dir):
                    from branchclaw.service import BranchClawService

                    service = BranchClawService()
                    service._reconcile_workers_local(run_id, worker_names=worker_names or None)
                    projection = service._apply_worker_watchdog_policies_local(
                        run_id,
                        worker_names=worker_names or None,
                    )
                self._refresh_state()
                return {
                    "runId": run_id,
                    "ownerDataDir": data_dir,
                    "workers": len(projection.workers),
                    "pendingRecovery": projection.stats.pending_recovery_count,
                }

            if action == "stop_service":
                process_id = str(request.get("process_id", ""))
                record = next((item for item in self._status.processes if item.id == process_id), None)
                if record is None:
                    raise BranchClawDaemonError(f"Managed process '{process_id}' not found")
                self._terminate_record(record)
                self._status.processes = [item for item in self._status.processes if item.id != process_id]
                self._status.data_dirs = _rebuild_data_dirs(
                    self._status.processes,
                    {item.data_dir for item in self._status.data_dirs},
                )
                _save_status(self._status)
                return {"stopped": True, "processId": process_id}

            raise BranchClawDaemonError(f"Unknown daemon action '{action}'")


class BranchClawDaemonHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.read().decode("utf-8").strip()
        if not raw:
            response = {"ok": False, "error": "Empty daemon request"}
        else:
            try:
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise BranchClawDaemonError("Daemon request must be a JSON object")
                result = self.server.controller.dispatch(request, self.server)
                response = {"ok": True, "result": result}
            except Exception as exc:  # noqa: BLE001
                response = {"ok": False, "error": str(exc)}
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))


class BranchClawUnixServer(socketserver.UnixStreamServer):
    allow_reuse_address = True

    def __init__(self, server_address: str, controller: BranchClawDaemonController):
        self.controller = controller
        super().__init__(server_address, BranchClawDaemonHandler)


def run_daemon_server(socket_path: str, *, host: str = "", port: int = 0) -> None:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink(missing_ok=True)
    controller = BranchClawDaemonController(dashboard_host=host, dashboard_port=port)
    server = BranchClawUnixServer(str(path), controller)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        controller._stop_dashboard_server()
        server.server_close()
        path.unlink(missing_ok=True)
        status = _load_status()
        historical_dirs = {item.data_dir for item in status.data_dirs}
        status.running = False
        status.processes = []
        status.data_dirs = _rebuild_data_dirs([], historical_dirs)
        status.dashboard_running = False
        status.dashboard_host = ""
        status.dashboard_port = 0
        status.dashboard_url = ""
        _save_status(status)
