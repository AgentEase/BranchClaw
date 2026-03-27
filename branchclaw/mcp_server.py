"""HTTP MCP server exposing BranchClaw worker tools."""

from __future__ import annotations

import contextvars
import os
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from branchclaw.config import get_data_dir
from branchclaw.mcp_state import load_worker_mcp_session_from_token
from branchclaw.worker_tools import execute_worker_tool

_session_var: contextvars.ContextVar[Any] = contextvars.ContextVar("branchclaw_mcp_session", default=None)

mcp = FastMCP("BranchClaw Worker Tools", stateless_http=True, json_response=True)
mcp.settings.streamable_http_path = "/mcp"


def _current_session():
    session = _session_var.get()
    if session is None:
        raise RuntimeError("No authenticated BranchClaw MCP session")
    return session


async def _dispatch_tool(tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
    session = _current_session()
    return await execute_worker_tool(session, tool_name, arguments or {})


@mcp.tool(name="context.get_worker_context")
async def context_get_worker_context() -> dict[str, Any]:
    return await _dispatch_tool("context.get_worker_context")


@mcp.tool(name="project.detect")
async def project_detect(repo_root: str = ".") -> dict[str, Any]:
    return await _dispatch_tool("project.detect", {"repo_root": repo_root})


@mcp.tool(name="project.install_dependencies")
async def project_install_dependencies(repo_root: str = ".") -> dict[str, Any]:
    return await _dispatch_tool("project.install_dependencies", {"repo_root": repo_root})


@mcp.tool(name="service.start_tmux")
async def service_start_tmux(
    command: list[str],
    session_name: str = "",
    window_name: str = "",
    cwd: str = ".",
    log_path: str = ".branchclaw-preview.log",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    return await _dispatch_tool(
        "service.start_tmux",
        {
            "command": command,
            "session_name": session_name,
            "window_name": window_name,
            "cwd": cwd,
            "log_path": log_path,
            "env": env or {},
        },
    )


@mcp.tool(name="service.discover_url")
async def service_discover_url(log_path: str = ".branchclaw-preview.log", timeout_seconds: float = 30.0) -> dict[str, Any]:
    return await _dispatch_tool(
        "service.discover_url",
        {
            "log_path": log_path,
            "timeout_seconds": timeout_seconds,
        },
    )


@mcp.tool(name="service.stop_tmux")
async def service_stop_tmux(target: str = "") -> dict[str, Any]:
    return await _dispatch_tool("service.stop_tmux", {"target": target})


@mcp.tool(name="diff.generate_architecture_summary")
async def diff_generate_architecture_summary(repo_root: str = ".", base_ref: str = "HEAD", head_ref: str = "") -> dict[str, Any]:
    return await _dispatch_tool(
        "diff.generate_architecture_summary",
        {
            "repo_root": repo_root,
            "base_ref": base_ref,
            "head_ref": head_ref,
        },
    )


@mcp.tool(name="worker.create_checkpoint")
async def worker_create_checkpoint(message: str = "") -> dict[str, Any]:
    return await _dispatch_tool("worker.create_checkpoint", {"message": message})


@mcp.tool(name="worker.report_result")
async def worker_report_result(
    status: str,
    changed_surface_summary: str = "",
    architecture_summary: str = "",
    stack: str = "",
    runtime: str = "",
    package_manager: str = "",
    install_command: str = "",
    start_command: str = "",
    preview_url: str = "",
    backend_url: str = "",
    output_snippet: str = "",
    warnings: list[str] | None = None,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    return await _dispatch_tool(
        "worker.report_result",
        {
            "status": status,
            "changed_surface_summary": changed_surface_summary,
            "architecture_summary": architecture_summary,
            "stack": stack,
            "runtime": runtime,
            "package_manager": package_manager,
            "install_command": install_command,
            "start_command": start_command,
            "preview_url": preview_url,
            "backend_url": backend_url,
            "output_snippet": output_snippet,
            "warnings": warnings or [],
            "blockers": blockers or [],
        },
    )


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        session = load_worker_mcp_session_from_token(auth.split(" ", 1)[1].strip())
        if session is None:
            return JSONResponse({"error": "invalid or revoked BranchClaw MCP token"}, status_code=401)
        token = _session_var.set(session)
        try:
            return await call_next(request)
        finally:
            _session_var.reset(token)


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def _healthz(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "server": "branchclaw-mcp",
            "data_dir": str(get_data_dir().resolve()),
            "pid": os.getpid(),
        }
    )


def build_starlette_app() -> Starlette:
    app = mcp.streamable_http_app()
    app.add_middleware(AuthMiddleware)
    return app


def run_server(host: str, port: int) -> None:
    uvicorn.run(build_starlette_app(), host=host, port=port, log_level="warning")
