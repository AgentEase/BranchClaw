"""Manual long-run harness for a real Three.js / R3F BranchClaw test."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from branchclaw.project_tools import discover_urls_from_text, generate_architecture_summary

DEFAULT_REPO_URL = "https://github.com/sanidhyy/threejs-portfolio.git"
DEFAULT_DURATION_HOURS = 24.0
DEFAULT_ITERATION_MINUTES = 210
DEFAULT_BASELINE_PORT = 4172
DEFAULT_WORKER_PORTS = {"worker-a": 4173, "worker-b": 4174}
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_INACTIVITY_MINUTES = 20
INTEGRATION_BRANCH = "longrun/integration"


class ThreejsLongrunError(RuntimeError):
    """Raised when the manual long-run harness cannot continue."""


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    log_prefix: Path


@dataclass
class ProbeResult:
    url: str
    screenshot_path: str = ""
    console_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    title: str = ""
    body_text_sample: str = ""
    ok: bool = False


@dataclass
class IterationRecord:
    label: str
    archive_id: str = ""
    approved: bool = False
    integrated: bool = False
    build_ok: bool = False
    lint_ok: bool | None = None
    regression: bool = False
    restore_archive_id: str = ""
    worker_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    preview_urls: dict[str, str] = field(default_factory=dict)
    probe_results: dict[str, ProbeResult] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def recommend_continue(self) -> bool:
        if self.regression or not self.build_ok:
            return False
        return all(result.ok for result in self.probe_results.values()) if self.probe_results else False


def iteration_label(index: int) -> str:
    return f"iter-{index:02d}"


def default_max_iterations(duration_hours: float) -> int:
    if duration_hours >= 48:
        return 12
    if duration_hours >= 24:
        return 6
    return max(1, int(round(duration_hours * 2)))


def dummy_env_local() -> str:
    return textwrap.dedent(
        """\
        VITE_APP_SERVICE_ID=service_dummy_branchclaw
        VITE_APP_TEMPLATE_ID=template_dummy_branchclaw
        VITE_APP_EMAIL=branchclaw@example.com
        VITE_APP_PUBLIC_KEY=public_dummy_branchclaw
        """
    )


def build_spec_text() -> str:
    return textwrap.dedent(
        """\
        This is a real BranchClaw long-run test on a React Three Fiber portfolio project.
        Prioritize visual upgrades, preserve the core information architecture, and keep the
        site navigable and responsive. Every iteration must leave behind preview URLs,
        screenshots, worker reports, architecture summaries, and an approved archive.
        """
    ).strip()


def build_rules_text() -> str:
    return textwrap.dedent(
        """\
        - Do not break existing navigation, primary sections, or 3D scene interactivity.
        - Preserve responsive behavior for desktop and mobile layouts.
        - Install dependencies in each worker workspace before running the app.
        - Run the Vite dev server on the assigned fixed port for that worker.
        - Publish a structured worker result before stopping work.
        - Keep changes reviewable; if blocked, report blockers explicitly instead of guessing.
        """
    ).strip()


def build_iteration_plan(iter_label: str, previous_summary: str = "") -> str:
    parts = [
        f"Iteration {iter_label} for a real three.js / React Three Fiber portfolio.",
        "Worker A owns scene visuals: lighting, material richness, scene layering, hero composition, and camera feel.",
        "Worker B owns page polish: section rhythm, typography, transitions, loading states, and mobile presentation.",
        "Both workers must preserve existing structure and report preview URLs plus architecture summaries before stopping.",
    ]
    if previous_summary.strip():
        parts.extend(["Previous iteration context:", previous_summary.strip()])
    return "\n".join(parts)


def build_worker_task(worker_name: str, port: int, iter_label: str, previous_summary: str = "") -> str:
    common = (
        f"This is {iter_label}. Use the injected BranchClaw MCP tools to inspect the worker context, "
        f"install dependencies, run the Vite app on port {port}, discover the actual preview URL, "
        "and publish a structured worker result. Choose the next step based on current repo state "
        "instead of following a rigid script order. Keep the assigned visual area moving without "
        "breaking the rest of the portfolio."
    )
    if worker_name == "worker-a":
        role = (
            "Focus on the 3D experience: hero scene composition, lighting, materials, camera motion, "
            "layering, and overall scene atmosphere."
        )
    else:
        role = (
            "Focus on surface polish: spacing, typography rhythm, section transitions, loading/fallback "
            "states, and mobile-friendly presentation."
        )
    if previous_summary.strip():
        return f"{common} {role} Previous iteration notes: {previous_summary.strip()}"
    return f"{common} {role}"


def safe_slug(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._")
    return safe or "item"


_IGNORED_SURFACE_FILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}


def summarize_changed_surface(worker_name: str, changed_files: Iterable[str]) -> str:
    relevant = [
        path
        for path in changed_files
        if path and Path(path).name not in _IGNORED_SURFACE_FILES
    ]
    if not relevant:
        if worker_name == "worker-a":
            return "Auto-reported before completion; no reviewable 3D scene diff was captured."
        if worker_name == "worker-b":
            return "Auto-reported before completion; no reviewable surface polish diff was captured."
        return "Auto-reported before completion; no reviewable diff was captured."
    sample = ", ".join(f"`{path}`" for path in relevant[:4])
    suffix = "" if len(relevant) <= 4 else f", and {len(relevant) - 4} more file(s)"
    if worker_name == "worker-a":
        return f"Auto-reported in-progress 3D scene work touching {sample}{suffix}."
    if worker_name == "worker-b":
        return f"Auto-reported in-progress UI polish touching {sample}{suffix}."
    return f"Auto-reported in-progress changes touching {sample}{suffix}."


def fallback_report_status(*, changed_files: Iterable[str], preview_url: str) -> str:
    has_changes = any(
        path and Path(path).name not in _IGNORED_SURFACE_FILES
        for path in changed_files
    )
    return "warning" if preview_url or has_changes else "blocked"


class ThreejsLongrunHarness:
    """Manual long-run harness using BranchClaw on a real R3F repository."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        repo_url: str = DEFAULT_REPO_URL,
        duration_hours: float = DEFAULT_DURATION_HOURS,
        iteration_minutes: int = DEFAULT_ITERATION_MINUTES,
        max_iterations: int | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        inactivity_minutes: int = DEFAULT_INACTIVITY_MINUTES,
    ):
        self.artifact_root = artifact_root
        self.repo_url = repo_url
        self.duration_hours = duration_hours
        self.iteration_minutes = iteration_minutes
        self.max_iterations = max_iterations or default_max_iterations(duration_hours)
        self.poll_seconds = poll_seconds
        self.inactivity_seconds = inactivity_minutes * 60
        self.command_index = 0
        self.run_id = ""
        self.repo_path = artifact_root / "repo"
        self.log_root = artifact_root / "logs"
        self.integration_branch = INTEGRATION_BRANCH
        self.iterations: list[IterationRecord] = []
        self.baseline_console_errors = 0
        self.best_iteration: IterationRecord | None = None
        self.failure_message = ""

    def run(self) -> int:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.log_root.mkdir(parents=True, exist_ok=True)
        try:
            self._preflight()
            self._clone_repo()
            self._prepare_repo()
            baseline = self._capture_baseline()
            self.run_id = self._create_branchclaw_run()
            previous_summary = baseline["summary"]

            for index in range(1, self.max_iterations + 1):
                record = self._run_iteration(index, previous_summary)
                self.iterations.append(record)
                self._write_iteration_summary(record)
                previous_summary = self._iteration_summary_text(record)
                if record.recommend_continue:
                    self.best_iteration = record
                if time.time() >= self._deadline:
                    break
        except Exception as exc:
            self.failure_message = str(exc)
            raise
        finally:
            self._finalize()
        return 0

    @property
    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["BRANCHCLAW_DATA_DIR"] = str(self.artifact_root / ".branchclaw")
        env["BRANCHCLAW_SKIP_PERMISSIONS"] = "1"
        env["BRANCHCLAW_SUPERVISOR_START_TIMEOUT"] = "90"
        env["BRANCHCLAW_CLAUDE_READY_TIMEOUT"] = "45"
        return env

    @property
    def _deadline(self) -> float:
        return self._start_time + (self.duration_hours * 3600.0)

    def _preflight(self) -> None:
        self._start_time = time.time()
        log_dir = self.log_root / "preflight"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._run(["claude", "--version"], log_dir=log_dir, label="claude-version")
        self._run(["tmux", "-V"], log_dir=log_dir, label="tmux-version")
        self._run(["node", "--version"], log_dir=log_dir, label="node-version")
        self._run(["npm", "--version"], log_dir=log_dir, label="npm-version")
        chrome = self._chrome_bin()
        self._run([chrome, "--version"], log_dir=log_dir, label="chrome-version")
        self._run([sys.executable, "-m", "branchclaw", "--help"], log_dir=log_dir, label="branchclaw-help")

    def _chrome_bin(self) -> str:
        for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            path = shutil.which(candidate)
            if path:
                return path
        raise ThreejsLongrunError("No supported Chrome/Chromium binary found for browser capture")

    def _clone_repo(self) -> None:
        log_dir = self.log_root / "clone"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._run(
            ["git", "clone", "--depth", "1", self.repo_url, str(self.repo_path)],
            log_dir=log_dir,
            label="git-clone",
        )

    def _prepare_repo(self) -> None:
        log_dir = self.log_root / "repo-setup"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._run(["git", "config", "user.email", "threejs-longrun@example.com"], log_dir=log_dir, label="git-email", cwd=self.repo_path)
        self._run(["git", "config", "user.name", "BranchClaw Three.js Harness"], log_dir=log_dir, label="git-name", cwd=self.repo_path)
        (self.repo_path / ".env.local").write_text(dummy_env_local(), encoding="utf-8")
        self._run(["git", "checkout", "-B", self.integration_branch], log_dir=log_dir, label="integration-branch", cwd=self.repo_path)

    def _capture_baseline(self) -> dict[str, Any]:
        log_dir = self.artifact_root / "baseline"
        log_dir.mkdir(parents=True, exist_ok=True)
        install = self._run(["npm", "install", "--legacy-peer-deps"], log_dir=log_dir, label="npm-install", cwd=self.repo_path, timeout=1800)
        build = self._run(["npm", "run", "build"], log_dir=log_dir, label="npm-build", cwd=self.repo_path, timeout=1800)
        lint = self._run(["npm", "run", "lint"], log_dir=log_dir, label="npm-lint", cwd=self.repo_path, check=False, timeout=1800)
        server = self._start_dev_server(self.repo_path, DEFAULT_BASELINE_PORT, log_dir / "baseline-dev.log")
        try:
            desktop = self._capture_probe(server.url, log_dir / "desktop", "baseline-desktop", mobile=False)
            mobile = self._capture_probe(server.url, log_dir / "mobile", "baseline-mobile", mobile=True)
        finally:
            self._stop_dev_server(server)

        self.baseline_console_errors = max(desktop.error_count, mobile.error_count)
        summary = textwrap.dedent(
            f"""\
            Baseline build={'pass' if build.returncode == 0 else 'fail'}.
            Baseline lint={'pass' if lint.returncode == 0 else 'soft-fail'}.
            Baseline preview={server.url}.
            Baseline console errors={self.baseline_console_errors}.
            """
        ).strip()
        (log_dir / "summary.md").write_text(summary + "\n", encoding="utf-8")
        return {
            "install": install.returncode == 0,
            "build": build.returncode == 0,
            "lint": lint.returncode == 0,
            "summary": summary,
        }

    def _create_branchclaw_run(self) -> str:
        name = f"threejs-visual-longrun-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        result = self._run_branchclaw_cli(
            [
                "--json",
                "run",
                "create",
                name,
                "--repo",
                str(self.repo_path),
                "--project-profile",
                "web",
                "--spec",
                build_spec_text(),
                "--rules",
                build_rules_text(),
            ],
            log_dir=self.log_root / "branchclaw",
            label="run-create",
        )
        return self._parse_json(result)["id"]

    def _run_iteration(self, index: int, previous_summary: str) -> IterationRecord:
        label = iteration_label(index)
        log_dir = self.artifact_root / "iterations" / label
        log_dir.mkdir(parents=True, exist_ok=True)
        record = IterationRecord(label=label)

        propose = self._run_branchclaw_cli(
            [
                "--json",
                "planner",
                "propose",
                self.run_id,
                build_iteration_plan(label, previous_summary),
                "--summary",
                label,
            ],
            log_dir=log_dir,
            label="planner-propose",
            timeout=300,
        )
        gate_id = self._parse_json(propose)["gateId"]
        self._run_branchclaw_cli(
            ["--json", "planner", "approve", self.run_id, gate_id, "--actor", "threejs-harness"],
            log_dir=log_dir,
            label="planner-approve",
            timeout=300,
        )

        for worker_name, port in DEFAULT_WORKER_PORTS.items():
            self._run_branchclaw_cli(
                [
                    "--json",
                    "worker",
                    "spawn",
                    self.run_id,
                    worker_name,
                    "--backend",
                    "tmux",
                    "--task",
                    build_worker_task(worker_name, port, label, previous_summary),
                    "--skip-permissions",
                    "claude",
                ],
                log_dir=log_dir,
                label=f"spawn-{worker_name}",
                timeout=180,
            )

        state = self._wait_for_iteration_state(log_dir, label)
        record.worker_results = {
            item["worker_name"]: item.get("result") or {}
            for item in state["workers"]
        }
        record.preview_urls = {
            item["worker_name"]: (item.get("result", {}) or {}).get("preview_url", "")
            for item in state["workers"]
        }

        for worker_name, preview_url in record.preview_urls.items():
            if preview_url:
                record.probe_results[f"{worker_name}-desktop"] = self._capture_probe(
                    preview_url,
                    log_dir / "previews" / worker_name / "desktop",
                    f"{worker_name}-desktop",
                    mobile=False,
                )
                record.probe_results[f"{worker_name}-mobile"] = self._capture_probe(
                    preview_url,
                    log_dir / "previews" / worker_name / "mobile",
                    f"{worker_name}-mobile",
                    mobile=True,
                )

        for worker_name in DEFAULT_WORKER_PORTS:
            self._run_branchclaw_cli(
                ["--json", "worker", "stop", self.run_id, worker_name],
                log_dir=log_dir,
                label=f"stop-{worker_name}",
                timeout=120,
            )
        self._run_branchclaw_cli(
            ["--json", "worker", "reconcile", self.run_id],
            log_dir=log_dir,
            label="worker-reconcile",
            timeout=120,
        )

        archive = self._run_branchclaw_cli(
            ["--json", "archive", "create", self.run_id, "--label", label, "--summary", f"Visual iteration {label}"],
            log_dir=log_dir,
            label="archive-create",
            timeout=120,
        )
        archive_payload = self._parse_json(archive)
        record.archive_id = archive_payload["archiveId"]
        self._run_branchclaw_cli(
            ["--json", "planner", "approve", self.run_id, archive_payload["gateId"], "--actor", "threejs-harness"],
            log_dir=log_dir,
            label="archive-approve",
            timeout=120,
        )
        record.approved = True

        record.integrated = self._integrate_archive(record.archive_id, log_dir, label)
        record.build_ok = self._run(["npm", "run", "build"], log_dir=log_dir / "integration", label="integration-build", cwd=self.repo_path, check=False, timeout=1800).returncode == 0
        lint_result = self._run(["npm", "run", "lint"], log_dir=log_dir / "integration", label="integration-lint", cwd=self.repo_path, check=False, timeout=1800)
        record.lint_ok = lint_result.returncode == 0

        integration_server = self._start_dev_server(self.repo_path, DEFAULT_BASELINE_PORT, log_dir / "integration" / "integration-dev.log")
        try:
            record.probe_results["integration-desktop"] = self._capture_probe(
                integration_server.url,
                log_dir / "integration" / "desktop",
                "integration-desktop",
                mobile=False,
            )
            record.probe_results["integration-mobile"] = self._capture_probe(
                integration_server.url,
                log_dir / "integration" / "mobile",
                "integration-mobile",
                mobile=True,
            )
        finally:
            self._stop_dev_server(integration_server)

        severe_errors = max(
            probe.error_count
            for key, probe in record.probe_results.items()
            if key.startswith("integration-")
        )
        if severe_errors > self.baseline_console_errors + 1 or not record.build_ok:
            record.regression = True
            previous_archive = self.best_iteration.archive_id if self.best_iteration else ""
            if previous_archive:
                self._rollback_to_archive(previous_archive, self.best_iteration.label, log_dir)
                record.restore_archive_id = previous_archive
                record.notes.append(f"Rolled back to {previous_archive} after regression")
            else:
                record.notes.append("Regression detected on first iteration; no previous archive available")
        else:
            accepted_branch = f"longrun/accepted/{label}"
            self._run(["git", "checkout", "-B", accepted_branch], log_dir=log_dir / "integration", label="accepted-branch", cwd=self.repo_path)
            self._run(["git", "checkout", self.integration_branch], log_dir=log_dir / "integration", label="restore-integration-branch", cwd=self.repo_path)

        self._run_branchclaw_cli(
            ["--json", "event", "export", self.run_id, "--out", str(log_dir / "event-export.json")],
            log_dir=log_dir,
            label="event-export",
            timeout=300,
        )
        self._run_branchclaw_cli(
            ["--json", "run", "show", self.run_id],
            log_dir=log_dir,
            label="run-show",
            timeout=300,
        )
        return record

    def _wait_for_iteration_state(self, log_dir: Path, label: str) -> dict[str, Any]:
        deadline = min(time.time() + self.iteration_minutes * 60, self._deadline)
        last_payload: dict[str, Any] | None = None
        last_activity: dict[str, float] = {}
        while time.time() < deadline:
            result = self._run_branchclaw_cli(
                ["--json", "run", "show", self.run_id],
                log_dir=log_dir,
                label=f"run-show-poll-{label}",
                timeout=180,
            )
            payload = self._parse_json(result)
            last_payload = payload
            workers = payload.get("workers", [])
            all_reported = len(workers) == 2 and all((item.get("result") or {}).get("status") for item in workers)
            for item in workers:
                mtime = self._workspace_activity(Path(item["workspace_path"]))
                previous = last_activity.get(item["worker_name"], 0.0)
                if mtime > previous:
                    last_activity[item["worker_name"]] = mtime
            now = time.time()
            inactive = [
                name
                for name, stamp in last_activity.items()
                if stamp and now - stamp > self.inactivity_seconds
            ]
            stale = [item["worker_name"] for item in workers if item["status"] in {"stale", "failed"}]
            if stale or inactive:
                self._run_branchclaw_cli(
                    ["--json", "worker", "reconcile", self.run_id],
                    log_dir=log_dir,
                    label=f"reconcile-{label}",
                    timeout=120,
                )
            if all_reported:
                return payload
            time.sleep(self.poll_seconds)
        if last_payload is None:
            raise ThreejsLongrunError("Iteration polling never produced a run payload")
        return self._maybe_autoreport_workers(last_payload, log_dir, label)

    def _maybe_autoreport_workers(self, payload: dict[str, Any], log_dir: Path, label: str) -> dict[str, Any]:
        missing = [
            item
            for item in payload.get("workers", [])
            if not (item.get("result") or {}).get("status")
        ]
        if not missing:
            return payload
        for item in missing:
            self._submit_fallback_worker_report(item, log_dir, label)
        refreshed = self._latest_run_snapshot(log_dir)
        return refreshed

    def _submit_fallback_worker_report(self, item: dict[str, Any], log_dir: Path, label: str) -> None:
        worker_name = item["worker_name"]
        workspace_path = Path(item["workspace_path"])
        preview_url = self._fallback_preview_url(workspace_path, item.get("tmux_target", ""))
        changed_files = self._git_changed_files(workspace_path)
        architecture_summary = generate_architecture_summary(workspace_path, base_ref="HEAD")
        changed_surface = summarize_changed_surface(worker_name, changed_files)
        status = fallback_report_status(changed_files=changed_files, preview_url=preview_url)
        warnings = [
            f"Auto-reported by the threejs harness after {label} reached its report deadline.",
        ]
        blockers: list[str] = []

        requested_port = DEFAULT_WORKER_PORTS.get(worker_name)
        actual_port = urlparse(preview_url).port if preview_url else None
        if requested_port and actual_port and actual_port != requested_port:
            warnings.append(
                f"Vite moved the preview from port {requested_port} to {actual_port} because the requested port was occupied."
            )
        if status == "blocked":
            blockers.append("No preview URL or reviewable diff was captured before the worker was stopped.")

        args = [
            "--json",
            "worker",
            "report",
            self.run_id,
            worker_name,
            "--source",
            "fallback",
            "--status",
            status,
            "--stack",
            "node",
            "--runtime",
            "node",
            "--package-manager",
            "npm",
            "--install-command",
            "npm install --legacy-peer-deps",
            "--start-command",
            f"npm run dev -- --host 127.0.0.1 --port {DEFAULT_WORKER_PORTS.get(worker_name, DEFAULT_BASELINE_PORT)}",
            "--changed-surface-summary",
            changed_surface,
            "--architecture-summary",
            architecture_summary,
        ]
        if preview_url:
            args.extend(["--preview-url", preview_url])
        output_snippet = self._tmux_output_snippet(item.get("tmux_target", ""))
        if output_snippet:
            args.extend(["--output-snippet", output_snippet])
        for warning in warnings:
            args.extend(["--warning", warning])
        for blocker in blockers:
            args.extend(["--blocker", blocker])
        self._run_branchclaw_cli(
            args,
            log_dir=log_dir,
            label=f"autoreport-{safe_slug(worker_name)}",
            timeout=180,
        )

    def _workspace_activity(self, workspace_path: Path) -> float:
        latest = 0.0
        for path in workspace_path.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {".git", "node_modules", "dist"} for part in path.parts):
                continue
            try:
                latest = max(latest, path.stat().st_mtime)
            except FileNotFoundError:
                continue
        return latest

    def _git_changed_files(self, workspace_path: Path) -> list[str]:
        tracked = self._run(
            ["git", "diff", "--name-only", "HEAD"],
            log_dir=self.log_root / "tmp",
            label=f"changed-files-{safe_slug(workspace_path.name)}",
            cwd=workspace_path,
            check=False,
            timeout=60,
        )
        untracked = self._run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            log_dir=self.log_root / "tmp",
            label=f"untracked-files-{safe_slug(workspace_path.name)}",
            cwd=workspace_path,
            check=False,
            timeout=60,
        )
        files: list[str] = []
        for raw in [*tracked.stdout.splitlines(), *untracked.stdout.splitlines()]:
            path = raw.strip()
            if path and path not in files:
                files.append(path)
        return files

    def _fallback_preview_url(self, workspace_path: Path, tmux_target: str) -> str:
        log_root = workspace_path / ".branchclaw"
        if log_root.exists():
            for path in sorted(log_root.rglob("*.log")):
                text = path.read_text(encoding="utf-8", errors="ignore")
                urls = discover_urls_from_text(text)
                if urls:
                    return urls[-1]
        pane_text = self._tmux_pane_text(tmux_target)
        urls = discover_urls_from_text(pane_text)
        return urls[-1] if urls else ""

    def _tmux_output_snippet(self, tmux_target: str, *, limit: int = 400) -> str:
        text = self._tmux_pane_text(tmux_target)
        if not text:
            return ""
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1] + "…"

    def _tmux_pane_text(self, tmux_target: str, *, lines: int = 200) -> str:
        if not tmux_target or not shutil.which("tmux"):
            return ""
        result = subprocess.run(
            ["tmux", "capture-pane", "-pt", tmux_target, "-S", f"-{lines}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return ""
        return result.stdout

    def _integrate_archive(self, archive_id: str, log_dir: Path, label: str) -> bool:
        snapshot = self._latest_run_snapshot(log_dir)
        archive = next((item for item in snapshot["archives"] if item["id"] == archive_id), None)
        if archive is None:
            raise ThreejsLongrunError(f"Archive {archive_id} not found in run snapshot")

        git_log_dir = log_dir / "integration"
        git_log_dir.mkdir(parents=True, exist_ok=True)
        self._run(["git", "checkout", self.integration_branch], log_dir=git_log_dir, label="checkout-integration", cwd=self.repo_path)
        for workspace in archive.get("workspaces", []):
            merge = self._run(
                ["git", "merge", "--no-edit", workspace["branch"]],
                log_dir=git_log_dir,
                label=f"merge-{safe_slug(workspace['worker_name'])}",
                cwd=self.repo_path,
                check=False,
                timeout=300,
            )
            if merge.returncode != 0:
                self._run(["git", "merge", "--abort"], log_dir=git_log_dir, label=f"merge-abort-{safe_slug(workspace['worker_name'])}", cwd=self.repo_path, check=False)
                raise ThreejsLongrunError(f"Local integration merge failed for {label}: {workspace['branch']}")
        return True

    def _rollback_to_archive(self, archive_id: str, archive_label: str, log_dir: Path) -> None:
        restore = self._run_branchclaw_cli(
            ["--json", "archive", "restore", self.run_id, archive_id, "--actor", "threejs-harness"],
            log_dir=log_dir,
            label="archive-restore-request",
            timeout=120,
        )
        restore_gate = self._parse_json(restore)["id"]
        self._run_branchclaw_cli(
            ["--json", "planner", "approve", self.run_id, restore_gate, "--actor", "threejs-harness"],
            log_dir=log_dir,
            label="archive-restore-approve",
            timeout=120,
        )
        accepted_branch = f"longrun/accepted/{archive_label}"
        self._run(["git", "checkout", "-B", self.integration_branch, accepted_branch], log_dir=log_dir / "integration", label="reset-integration", cwd=self.repo_path)

    def _latest_run_snapshot(self, log_dir: Path) -> dict[str, Any]:
        result = self._run_branchclaw_cli(
            ["--json", "run", "show", self.run_id],
            log_dir=log_dir,
            label="run-show-latest",
            timeout=120,
        )
        return self._parse_json(result)

    def _capture_probe(self, url: str, out_dir: Path, label: str, *, mobile: bool) -> ProbeResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        preset = "mobile" if mobile else "desktop"
        result = self._run(
            [
                "node",
                str(Path(__file__).resolve().parents[1] / "scripts" / "chrome_probe.mjs"),
                "--url",
                url,
                "--out-dir",
                str(out_dir),
                "--label",
                label,
                "--preset",
                preset,
            ],
            log_dir=out_dir,
            label=f"probe-{label}",
            timeout=180,
        )
        payload = self._parse_json(result)
        return ProbeResult(
            url=payload["url"],
            screenshot_path=payload.get("screenshotPath", ""),
            console_count=payload.get("consoleCount", 0),
            warning_count=payload.get("warningCount", 0),
            error_count=payload.get("errorCount", 0),
            title=payload.get("title", ""),
            body_text_sample=payload.get("bodyTextSample", ""),
            ok=payload.get("ok", False),
        )

    def _write_iteration_summary(self, record: IterationRecord) -> None:
        path = self.artifact_root / "iterations" / record.label / "summary.md"
        lines = [
            f"# {record.label}",
            "",
            f"- Archive ID: {record.archive_id or '(none)'}",
            f"- Approved: {'yes' if record.approved else 'no'}",
            f"- Integrated: {'yes' if record.integrated else 'no'}",
            f"- Build: {'pass' if record.build_ok else 'fail'}",
            f"- Lint: {'pass' if record.lint_ok else 'soft-fail'}",
            f"- Regression: {'yes' if record.regression else 'no'}",
            f"- Recommended Continue: {'yes' if record.recommend_continue else 'no'}",
        ]
        for worker_name, preview in sorted(record.preview_urls.items()):
            if preview:
                lines.append(f"- {worker_name} preview: {preview}")
        if record.restore_archive_id:
            lines.append(f"- Restored To: {record.restore_archive_id}")
        if record.notes:
            lines.extend(["", "## Notes"])
            lines.extend(f"- {note}" for note in record.notes)
        lines.extend(["", "## Worker Results"])
        for worker_name, payload in sorted(record.worker_results.items()):
            lines.append(
                f"- {worker_name}: {payload.get('status', '-')} / "
                f"{payload.get('changed_surface_summary', '(no summary)')}"
            )
        lines.extend(["", "## Probes"])
        for key, probe in sorted(record.probe_results.items()):
            lines.append(
                f"- {key}: ok={'yes' if probe.ok else 'no'} "
                f"errors={probe.error_count} warnings={probe.warning_count} screenshot={probe.screenshot_path or '-'}"
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _iteration_summary_text(self, record: IterationRecord) -> str:
        snippets = [
            f"{record.label}: build={'pass' if record.build_ok else 'fail'}",
        ]
        for worker_name, payload in sorted(record.worker_results.items()):
            summary = payload.get("changed_surface_summary") or "(no worker summary)"
            snippets.append(f"{worker_name}: {summary}")
        return " | ".join(snippets)

    def _finalize(self) -> None:
        final_dir = self.artifact_root / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        if self.run_id:
            self._run_branchclaw_cli(
                ["--json", "event", "export", self.run_id, "--out", str(final_dir / "event-export.json")],
                log_dir=final_dir,
                label="event-export",
                timeout=300,
                check=False,
            )
            self._run_branchclaw_cli(
                ["--json", "run", "show", self.run_id],
                log_dir=final_dir,
                label="run-show-final",
                timeout=300,
                check=False,
            )
        best = self.best_iteration
        best_text = (
            f"# Best Archive\n\n- Label: {best.label}\n- Archive ID: {best.archive_id}\n"
            if best
            else "# Best Archive\n\n- No iteration met the acceptance threshold.\n"
        )
        (final_dir / "best-archive.md").write_text(best_text, encoding="utf-8")

        lines = [
            "# Manual Three.js Long-Run Summary",
            "",
            f"- Generated At: {datetime.now(timezone.utc).isoformat()}",
            f"- Repo URL: {self.repo_url}",
            f"- Run ID: {self.run_id}",
            f"- Integration Branch: {self.integration_branch}",
            f"- Iterations: {len(self.iterations)}",
        ]
        if self.failure_message:
            lines.append(f"- Failure: {self.failure_message}")
        if best:
            lines.append(f"- Best Archive: {best.archive_id} ({best.label})")
        else:
            lines.append("- Best Archive: none")
        lines.extend(["", "## Iterations"])
        for record in self.iterations:
            lines.append(
                f"- {record.label}: build={'pass' if record.build_ok else 'fail'} "
                f"recommended={'yes' if record.recommend_continue else 'no'} "
                f"archive={record.archive_id or '-'}"
            )
        (self.artifact_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _parse_json(self, result: CommandResult) -> Any:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ThreejsLongrunError(f"Expected JSON output from {' '.join(result.args)}") from exc

    def _run_branchclaw_cli(
        self,
        args: list[str],
        *,
        log_dir: Path,
        label: str,
        timeout: int | float | None = None,
        check: bool = True,
    ) -> CommandResult:
        return self._run(
            [sys.executable, "-m", "branchclaw", *args],
            log_dir=log_dir,
            label=label,
            cwd=self.repo_path,
            env=self.env,
            timeout=timeout,
            check=check,
        )

    def _run(
        self,
        args: list[str],
        *,
        log_dir: Path,
        label: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int | float | None = None,
        check: bool = True,
    ) -> CommandResult:
        self.command_index += 1
        prefix = log_dir / f"{self.command_index:03d}-{label}"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        command_text = " ".join(_shell_quote(part) for part in args)
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        (prefix.with_suffix(".cmd.txt")).write_text(command_text + "\n", encoding="utf-8")
        (prefix.with_suffix(".stdout.txt")).write_text(result.stdout, encoding="utf-8")
        (prefix.with_suffix(".stderr.txt")).write_text(result.stderr, encoding="utf-8")
        payload = CommandResult(
            args=args,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            log_prefix=prefix,
        )
        if check and result.returncode != 0:
            raise ThreejsLongrunError(
                f"Command failed ({result.returncode}): {command_text}\n{result.stderr or result.stdout}"
            )
        return payload

    def _start_dev_server(self, cwd: Path, port: int, log_path: Path) -> "_DevServerHandle":
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(cwd),
            env=self.env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        url = self._wait_for_http_url(log_path, port)
        return _DevServerHandle(process=process, log_handle=handle, log_path=log_path, url=url)

    def _wait_for_http_url(self, log_path: Path, port: int, timeout: int = 180) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if log_path.exists():
                text = log_path.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r"https?://127\.0\.0\.1:%d[^\s]*" % port, text)
                if match:
                    return match.group(0).rstrip(").,;")
                match = re.search(r"https?://localhost:%d[^\s]*" % port, text)
                if match:
                    return match.group(0).replace("localhost", "127.0.0.1").rstrip(").,;")
            if _port_open("127.0.0.1", port):
                return f"http://127.0.0.1:{port}"
            time.sleep(1)
        raise ThreejsLongrunError(f"Timed out waiting for dev server URL in {log_path}")

    def _stop_dev_server(self, handle: "_DevServerHandle") -> None:
        if handle.process.poll() is None:
            handle.process.terminate()
            try:
                handle.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                handle.process.kill()
                handle.process.wait(timeout=5)
        handle.log_handle.close()


@dataclass
class _DevServerHandle:
    process: subprocess.Popen[str]
    log_handle: Any
    log_path: Path
    url: str


def _shell_quote(value: str) -> str:
    if not value:
        return "''"
    if all(char.isalnum() or char in "-_./:=+" for char in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def print_summary(summary_path: Path, *, stream: Any = None) -> None:
    output = stream or sys.stdout
    print(f"[manual-threejs] Summary: {summary_path}", file=output)
    if summary_path.exists():
        print(summary_path.read_text(encoding="utf-8"), file=output)
