"""BranchClaw worker tool backend shared by MCP and helper wrappers."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, Callable

from branchclaw.models import WorkerMcpSession
from branchclaw.project_tools import (
    detect_project_stack,
    generate_architecture_summary,
    install_dependencies,
    launch_tmux_service,
    wait_for_url,
)
from branchclaw.runtime import terminate_tmux_target
from branchclaw.service import BranchClawService
from branchclaw.storage import EventStore
from branchclaw.workspace import GitWorkspaceRuntimeAdapter

ToolHandler = Callable[[WorkerMcpSession, dict[str, Any]], Any]


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except TypeError:
        return str(value)


def _resolve_worker_path(session: WorkerMcpSession, value: str, *, directory: bool = False) -> Path:
    raw = value or "."
    base = Path(session.workspace_path).resolve()
    candidate = (base / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if base == candidate or base in candidate.parents:
        if directory:
            candidate.mkdir(parents=True, exist_ok=True)
        else:
            candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    raise RuntimeError(f"Path escapes worker workspace: {value}")


def _tool_event_summary(tool_name: str, result: Any) -> dict[str, Any]:
    payload = _json_safe(result)
    if not isinstance(payload, dict):
        return {"result": payload}
    if tool_name == "service.start_tmux":
        return {
            "target": payload.get("target", ""),
            "log_path": payload.get("log_path", ""),
            "launch_command": payload.get("launch_command", ""),
        }
    if tool_name == "service.discover_url":
        return {
            "url": payload.get("url", ""),
            "log_path": payload.get("log_path", ""),
        }
    if tool_name == "worker.report_result":
        return {
            "report_source": payload.get("report_source", ""),
            "result_status": payload.get("status", ""),
            "preview_url": payload.get("preview_url", ""),
            "backend_url": payload.get("backend_url", ""),
        }
    if tool_name == "project.install_dependencies":
        return {
            "ok": payload.get("ok", False),
            "command": payload.get("command", []),
            "returncode": payload.get("returncode"),
            "stderr": str(payload.get("stderr", ""))[:240],
        }
    if tool_name == "diff.generate_architecture_summary":
        return {"summary": str(payload.get("summary", ""))[:240]}
    return payload


def _store_tool_called(session: WorkerMcpSession, tool_name: str, arguments: dict[str, Any]) -> None:
    EventStore().append(
        session.run_id,
        "worker.tool_called",
        {
            "worker_name": session.worker_name,
            "tool_name": tool_name,
            "arguments": _json_safe(arguments),
        },
    )


def _store_tool_completed(session: WorkerMcpSession, tool_name: str, result: Any) -> None:
    EventStore().append(
        session.run_id,
        "worker.tool_completed",
        {
            "worker_name": session.worker_name,
            "tool_name": tool_name,
            "result": _tool_event_summary(tool_name, result),
        },
    )


def _store_tool_failed(session: WorkerMcpSession, tool_name: str, arguments: dict[str, Any], error: Exception) -> None:
    try:
        diff_signature = GitWorkspaceRuntimeAdapter(session.repo_root).diff_signature(session.workspace_path)
    except Exception:
        diff_signature = ""
    EventStore().append(
        session.run_id,
        "worker.tool_failed",
        {
            "worker_name": session.worker_name,
            "tool_name": tool_name,
            "arguments": _json_safe(arguments),
            "error": str(error),
            "diff_signature": diff_signature,
        },
    )


def _get_worker_context(session: WorkerMcpSession, _arguments: dict[str, Any]) -> dict[str, Any]:
    service = BranchClawService()
    projection = service.get_run(session.run_id, rebuild=True)
    worker = projection.workers.get(session.worker_name)
    latest_archive = None
    if projection.archives:
        archive = sorted(projection.archives.values(), key=lambda item: item.created_at)[-1]
        latest_archive = {
            "id": archive.id,
            "label": archive.label,
            "summary": archive.summary,
            "status": archive.status.value,
            "created_at": archive.created_at,
        }
    current_plan = projection.plans.get(projection.run.active_plan_id)
    return {
        "run_id": session.run_id,
        "worker_name": session.worker_name,
        "task": session.task,
        "workspace_path": session.workspace_path,
        "stage_id": projection.run.current_stage_id,
        "run_status": projection.run.status.value,
        "project_profile": projection.run.project_profile.value,
        "spec_content": projection.run.spec_content,
        "rules_content": projection.run.rules_content,
        "constraints": [item.content for item in projection.constraints],
        "needs_replan": projection.run.needs_replan,
        "latest_archive": latest_archive,
        "active_plan": {
            "id": current_plan.id,
            "summary": current_plan.summary,
            "content": current_plan.content,
        }
        if current_plan
        else None,
        "existing_result": json.loads(worker.result.model_dump_json()) if worker and worker.result else None,
    }


def _project_detect(session: WorkerMcpSession, arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _resolve_worker_path(session, str(arguments.get("repo_root", ".")), directory=True)
    return detect_project_stack(repo_root)


def _project_install_dependencies(session: WorkerMcpSession, arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _resolve_worker_path(session, str(arguments.get("repo_root", ".")), directory=True)
    project_info = detect_project_stack(repo_root)
    result = install_dependencies(repo_root, project_info)
    if not result.get("ok"):
        raise RuntimeError(result.get("stderr") or "Dependency installation failed")
    return {
        **result,
        "stack": project_info.get("stack", ""),
        "runtime": project_info.get("runtime", ""),
        "package_manager": project_info.get("package_manager", ""),
    }


def _service_start_tmux(session: WorkerMcpSession, arguments: dict[str, Any]) -> dict[str, Any]:
    command = arguments.get("command") or []
    if not isinstance(command, list) or not command:
        raise RuntimeError("service.start_tmux requires a non-empty command list")
    cwd = _resolve_worker_path(session, str(arguments.get("cwd", ".")), directory=True)
    log_path = _resolve_worker_path(session, str(arguments.get("log_path", ".branchclaw-preview.log")))
    env = arguments.get("env") or {}
    if not isinstance(env, dict):
        raise RuntimeError("service.start_tmux env must be an object")
    session_name = str(arguments.get("session_name") or f"branchclaw-{session.run_id[:12]}")
    window_name = str(arguments.get("window_name") or session.worker_name)
    return launch_tmux_service(
        session_name=session_name,
        window_name=window_name,
        cwd=cwd,
        command=[str(item) for item in command],
        log_path=log_path,
        env={str(key): str(value) for key, value in env.items()},
    )


def _service_discover_url(session: WorkerMcpSession, arguments: dict[str, Any]) -> dict[str, Any]:
    log_path = _resolve_worker_path(session, str(arguments.get("log_path", ".branchclaw-preview.log")))
    timeout_seconds = float(arguments.get("timeout_seconds", 30.0))
    url = wait_for_url(log_path, timeout_seconds=timeout_seconds)
    if not url:
        raise RuntimeError(f"No preview URL discovered in {log_path}")
    return {
        "url": url,
        "log_path": str(log_path),
        "timeout_seconds": timeout_seconds,
    }


def _service_stop_tmux(session: WorkerMcpSession, arguments: dict[str, Any]) -> dict[str, Any]:
    target = str(arguments.get("target", "")).strip()
    if not target:
        projection = BranchClawService().get_run(session.run_id, rebuild=True)
        worker = projection.workers.get(session.worker_name)
        target = worker.active_service_target if worker else ""
    if not target:
        raise RuntimeError("service.stop_tmux requires a target or an active service target")
    terminate_tmux_target(target)
    return {"target": target, "stopped": True}


def _diff_generate_architecture_summary(session: WorkerMcpSession, arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _resolve_worker_path(session, str(arguments.get("repo_root", ".")), directory=True)
    base_ref = str(arguments.get("base_ref", "HEAD"))
    head_ref = str(arguments.get("head_ref", ""))
    return {
        "summary": generate_architecture_summary(repo_root, base_ref=base_ref, head_ref=head_ref)
    }


def _worker_create_checkpoint(session: WorkerMcpSession, arguments: dict[str, Any]) -> dict[str, Any]:
    message = str(arguments.get("message", "")).strip()
    worker = BranchClawService().checkpoint_worker(session.run_id, session.worker_name, message=message)
    return {
        "worker_name": worker.worker_name,
        "head_sha": worker.head_sha,
        "status": worker.status.value,
    }


def _worker_report_result(session: WorkerMcpSession, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in arguments.items()
        if key
        in {
            "status",
            "stack",
            "runtime",
            "package_manager",
            "install_command",
            "start_command",
            "preview_url",
            "backend_url",
            "output_snippet",
            "changed_surface_summary",
            "architecture_summary",
            "warnings",
            "blockers",
        }
        and value not in (None, "")
    }
    if "warnings" in payload and not isinstance(payload["warnings"], list):
        payload["warnings"] = [str(payload["warnings"])]
    if "blockers" in payload and not isinstance(payload["blockers"], list):
        payload["blockers"] = [str(payload["blockers"])]
    worker = BranchClawService().report_worker_result(
        session.run_id,
        session.worker_name,
        payload,
        source="agent",
    )
    result = worker.result.model_dump() if worker.result else {}
    return {
        **result,
        "worker_name": worker.worker_name,
        "report_source": worker.report_source or "agent",
    }


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "context.get_worker_context": _get_worker_context,
    "project.detect": _project_detect,
    "project.install_dependencies": _project_install_dependencies,
    "service.start_tmux": _service_start_tmux,
    "service.discover_url": _service_discover_url,
    "service.stop_tmux": _service_stop_tmux,
    "diff.generate_architecture_summary": _diff_generate_architecture_summary,
    "worker.create_checkpoint": _worker_create_checkpoint,
    "worker.report_result": _worker_report_result,
}


async def execute_worker_tool(
    session: WorkerMcpSession,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    if tool_name not in TOOL_HANDLERS:
        raise RuntimeError(f"Unknown worker tool '{tool_name}'")
    if tool_name not in session.allowed_tools:
        raise RuntimeError(f"Tool '{tool_name}' is not allowed for this worker")
    args = dict(arguments or {})
    handler = TOOL_HANDLERS[tool_name]
    _store_tool_called(session, tool_name, args)
    try:
        result = handler(session, args)
        if inspect.isawaitable(result):
            result = await result
        _store_tool_completed(session, tool_name, result)
        return result
    except Exception as exc:
        _store_tool_failed(session, tool_name, args, exc)
        raise


def execute_worker_tool_sync(
    session: WorkerMcpSession,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    return asyncio.run(execute_worker_tool(session, tool_name, arguments))
