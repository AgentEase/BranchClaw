"""Helper functions used by project-type development skills."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Callable

import tomllib

URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def detect_project_stack(repo_root: str | Path) -> dict[str, Any]:
    root = Path(repo_root)
    package_json = root / "package.json"
    pyproject = root / "pyproject.toml"
    requirements = root / "requirements.txt"
    frontend_markers = (
        root / "src",
        root / "app",
        root / "pages",
        root / "public",
        root / "next.config.js",
        root / "vite.config.ts",
        root / "vite.config.js",
        root / "astro.config.mjs",
    )
    backend_markers = (
        root / "server",
        root / "api",
        root / "manage.py",
        root / "main.py",
        root / "app.py",
    )

    info: dict[str, Any] = {
        "repo_root": str(root),
        "runtime": "",
        "stack": "unknown",
        "package_manager": "",
        "frontend": False,
        "backend": False,
        "manifests": [],
    }

    if package_json.exists():
        info["runtime"] = "node"
        info["stack"] = "node"
        info["manifests"].append("package.json")
        declared_package_manager = ""
        if (root / "pnpm-lock.yaml").exists():
            info["package_manager"] = "pnpm"
            info["manifests"].append("pnpm-lock.yaml")
        elif (root / "yarn.lock").exists():
            info["package_manager"] = "yarn"
            info["manifests"].append("yarn.lock")
        else:
            info["package_manager"] = "npm"
            if (root / "package-lock.json").exists():
                info["manifests"].append("package-lock.json")

        try:
            pkg = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            pkg = {}
        scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
        declared_package_manager = str(pkg.get("packageManager", "")) if isinstance(pkg, dict) else ""
        if declared_package_manager.startswith("pnpm@"):
            info["package_manager"] = "pnpm"
        elif declared_package_manager.startswith("yarn@"):
            info["package_manager"] = "yarn"
        elif declared_package_manager.startswith("npm@"):
            info["package_manager"] = "npm"
        script_text = " ".join(str(value) for value in scripts.values())
        info["frontend"] = any(marker.exists() for marker in frontend_markers)
        info["backend"] = any(marker.exists() for marker in backend_markers) or any(
            token in script_text for token in ("express", "fastify", "nest", "node server", "tsx watch", "nodemon")
        )
        info["declared_package_manager"] = declared_package_manager
        return info

    if pyproject.exists() or requirements.exists() or (root / "manage.py").exists():
        info["runtime"] = "python"
        info["stack"] = "python"
        if pyproject.exists():
            info["manifests"].append("pyproject.toml")
        if requirements.exists():
            info["manifests"].append("requirements.txt")
        info["backend"] = True
        if pyproject.exists():
            try:
                parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except Exception:
                parsed = {}
            if "project" in parsed:
                info["package_manager"] = "uv"
            else:
                info["package_manager"] = "pip"
        elif requirements.exists():
            info["package_manager"] = "pip"
        return info

    return info


def build_install_command(
    repo_root: str | Path,
    project_info: dict[str, Any],
    *,
    which: Callable[[str], str | None] | None = None,
) -> list[str]:
    resolver = which or shutil.which
    root = Path(repo_root)
    runtime = project_info.get("runtime", "")
    package_manager = project_info.get("package_manager", "")

    if runtime == "node":
        if package_manager == "pnpm":
            return ["pnpm", "install"] if resolver("pnpm") else ["corepack", "pnpm", "install"]
        if package_manager == "yarn":
            return ["yarn", "install"] if resolver("yarn") else ["corepack", "yarn", "install"]
        return ["npm", "install"]

    if runtime == "python":
        if (root / "pyproject.toml").exists() and resolver("uv"):
            return ["uv", "sync"]
        if (root / "requirements.txt").exists():
            python_bin = resolver("python3") or sys.executable
            return [python_bin, "-m", "pip", "install", "-r", "requirements.txt"]
        return []

    return []


def install_dependencies(repo_root: str | Path, project_info: dict[str, Any]) -> dict[str, Any]:
    command = build_install_command(repo_root, project_info)
    if not command:
        return {
            "ok": False,
            "command": [],
            "stdout": "",
            "stderr": "No supported install command for detected project",
        }
    if command[:2] == ["corepack", "pnpm"] or command[:2] == ["corepack", "yarn"]:
        subprocess.run(["corepack", "enable"], cwd=str(repo_root), capture_output=True, text=True)
    attempted_commands: list[list[str]] = [command]
    result = subprocess.run(command, cwd=str(repo_root), capture_output=True, text=True)
    if (
        result.returncode != 0
        and command[:2] == ["npm", "install"]
        and "ERESOLVE" in f"{result.stdout}\n{result.stderr}"
    ):
        fallback_command = ["npm", "install", "--legacy-peer-deps"]
        attempted_commands.append(fallback_command)
        result = subprocess.run(
            fallback_command,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
    return {
        "ok": result.returncode == 0,
        "command": attempted_commands[-1],
        "attempted_commands": attempted_commands,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }


def launch_tmux_service(
    *,
    session_name: str,
    window_name: str,
    cwd: str | Path,
    command: list[str],
    log_path: str | Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is not installed")

    cwd_path = Path(cwd)
    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    export_clause = " ".join(
        f"{key}={shlex.quote(value)}"
        for key, value in sorted((env or {}).items())
        if value
    )
    base_cmd = " ".join(shlex.quote(part) for part in command)
    if export_clause:
        base_cmd = f"env {export_clause} {base_cmd}"
    full_cmd = (
        f"cd {shlex.quote(str(cwd_path))} && "
        f"({base_cmd}) 2>&1 | tee -a {shlex.quote(str(log_file))}"
    )

    session_exists = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
        text=True,
    )
    if session_exists.returncode == 0:
        launch = subprocess.run(
            ["tmux", "new-window", "-t", session_name, "-n", window_name, full_cmd],
            capture_output=True,
            text=True,
        )
    else:
        launch = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-n", window_name, full_cmd],
            capture_output=True,
            text=True,
        )
    if launch.returncode != 0:
        raise RuntimeError(launch.stderr.strip() or "Failed to launch tmux service")
    return {
        "session_name": session_name,
        "window_name": window_name,
        "target": f"{session_name}:{window_name}",
        "command": command,
        "log_path": str(log_file),
        "launch_command": full_cmd,
    }


def discover_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for match in URL_RE.findall(text):
        cleaned = match.rstrip(").,;")
        if cleaned not in urls:
            urls.append(cleaned)
    return urls


def wait_for_url(log_path: str | Path, *, timeout_seconds: float = 30.0, poll_interval: float = 0.5) -> str:
    log_file = Path(log_path)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if log_file.exists():
            urls = discover_urls_from_text(log_file.read_text(encoding="utf-8", errors="ignore"))
            if urls:
                return urls[0]
        time.sleep(poll_interval)
    return ""


def generate_architecture_summary(
    repo_root: str | Path,
    *,
    base_ref: str = "HEAD",
    head_ref: str = "",
) -> str:
    root = Path(repo_root)
    if head_ref:
        diff_cmd = ["git", "diff", "--name-status", f"{base_ref}..{head_ref}"]
        stat_cmd = ["git", "diff", "--stat", f"{base_ref}..{head_ref}"]
    else:
        diff_cmd = ["git", "diff", "--name-status", base_ref]
        stat_cmd = ["git", "diff", "--stat", base_ref]

    diff_result = subprocess.run(diff_cmd, cwd=str(root), capture_output=True, text=True)
    stat_result = subprocess.run(stat_cmd, cwd=str(root), capture_output=True, text=True)
    changes = [line.strip() for line in diff_result.stdout.splitlines() if line.strip()]
    if not changes:
        return "# Architecture Change Summary\n\n- No source changes detected.\n"

    top_level: list[str] = []
    file_lines: list[str] = []
    for line in changes:
        parts = line.split("\t")
        status = parts[0]
        path = parts[-1]
        area = path.split("/", 1)[0]
        if area not in top_level:
            top_level.append(area)
        file_lines.append(f"- `{status}` `{path}`")

    bullet_areas = ", ".join(f"`{item}`" for item in top_level)
    stat_text = stat_result.stdout.strip() or "No diff stats available."
    return textwrap.dedent(
        f"""\
        # Architecture Change Summary

        - Changed areas: {bullet_areas}
        - Changed files: {len(changes)}

        ## Diff Stat

        ```text
        {stat_text}
        ```

        ## File-Level Changes

        {chr(10).join(file_lines)}
        """
    ).strip() + "\n"


def emit_worker_report(
    result: dict[str, Any],
    *,
    run_id: str | None = None,
    worker_name: str | None = None,
) -> subprocess.CompletedProcess[str]:
    actual_run_id = run_id or os.environ.get("BRANCHCLAW_RUN_ID", "")
    actual_worker_name = worker_name or os.environ.get("BRANCHCLAW_WORKER_NAME", "")
    if not actual_run_id or not actual_worker_name:
        raise RuntimeError("BRANCHCLAW_RUN_ID and BRANCHCLAW_WORKER_NAME must be set")

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="branchclaw-worker-report-",
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
        report_path = handle.name

    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "branchclaw",
                "worker",
                "report",
                actual_run_id,
                actual_worker_name,
                "--source",
                "agent",
                "--result-file",
                report_path,
            ],
            capture_output=True,
            text=True,
        )
    finally:
        Path(report_path).unlink(missing_ok=True)
