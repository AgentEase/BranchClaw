#!/usr/bin/env python3
"""On-demand live Claude Code acceptance harness for BranchClaw and ClawTeam."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_MINUTES = 25


class HarnessError(RuntimeError):
    """Raised when a live acceptance step fails."""


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    log_prefix: Path


@dataclass
class ScenarioResult:
    name: str
    ok: bool = False
    notes: list[str] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)


class LiveClaudeAcceptance:
    def __init__(
        self,
        *,
        target: str,
        artifact_root: Path,
        timeout_minutes: int,
    ):
        self.target = target
        self.artifact_root = artifact_root
        self.timeout_seconds = timeout_minutes * 60
        self.temp_root = Path(tempfile.mkdtemp(prefix="live-claude-acceptance-"))
        self.command_index = 0
        self.results: list[ScenarioResult] = []

    def run(self) -> int:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        try:
            self._preflight()
            if self.target in {"branchclaw", "both"}:
                self.results.append(self._run_branchclaw())
            if self.target in {"clawteam", "both"}:
                self.results.append(self._run_clawteam())
        except Exception as exc:
            self._write_summary(error=str(exc))
            raise
        else:
            self._write_summary()
            return 0
        finally:
            self._cleanup_tmux_sessions(prefix="branchclaw-")
            self._cleanup_tmux_sessions(prefix="clawteam-")
            shutil.rmtree(self.temp_root, ignore_errors=True)

    def _preflight(self) -> None:
        log_dir = self.artifact_root / "preflight"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._run(["claude", "--version"], log_dir=log_dir, label="claude-version")
        self._run(["tmux", "-V"], log_dir=log_dir, label="tmux-version")
        self._run(
            [sys.executable, "-m", "branchclaw", "--help"],
            log_dir=log_dir,
            label="branchclaw-help",
        )
        self._run(
            [sys.executable, "-m", "clawteam", "--help"],
            log_dir=log_dir,
            label="clawteam-help",
        )
        self._standalone_tmux_claude_smoke(log_dir)

    def _run_branchclaw(self) -> ScenarioResult:
        result = ScenarioResult(name="branchclaw")
        log_dir = self.artifact_root / "branchclaw"
        log_dir.mkdir(parents=True, exist_ok=True)
        root = self.temp_root / "branchclaw"
        repo = self._init_seed_repo(root / "repo")
        env = self._branchclaw_env(root)

        create = self._run_branchclaw_cli(
            ["--json", "run", "create", "live-branchclaw", "--repo", str(repo), "--spec", "Live Claude acceptance", "--rules", "Keep changes minimal and isolated."],
            log_dir=log_dir,
            label="run-create",
            env=env,
            cwd=repo,
        )
        run_payload = self._parse_json(create)
        run_id = run_payload["id"]
        result.notes.append(f"run_id={run_id}")

        propose = self._run_branchclaw_cli(
            [
                "--json",
                "planner",
                "propose",
                run_id,
                "Worker A implements slugify with tests. Worker B implements word_count, updates README, and adds tests.",
                "--summary",
                "initial live claude plan",
            ],
            log_dir=log_dir,
            label="planner-propose",
            env=env,
            cwd=repo,
        )
        plan_gate = self._parse_json(propose)["gateId"]
        self._run_branchclaw_cli(
            ["--json", "planner", "approve", run_id, plan_gate, "--actor", "reviewer"],
            log_dir=log_dir,
            label="planner-approve",
            env=env,
            cwd=repo,
        )

        task_a = (
            "Implement slugify(value: str) in slugify.py, add tests/test_slugify.py, "
            "keep changes focused, and stop editing once the task is done."
        )
        task_b = (
            "Implement word_count(text: str) in word_count.py, add tests/test_word_count.py, "
            "update README.md with a short usage example, and stop editing once done."
        )
        self._run_branchclaw_cli(
            [
                "--json",
                "worker",
                "spawn",
                run_id,
                "worker-a",
                "--backend",
                "tmux",
                "--task",
                task_a,
                "--skip-permissions",
                "claude",
            ],
            log_dir=log_dir,
            label="spawn-worker-a",
            env=env,
            cwd=repo,
            timeout=120,
        )
        self._run_branchclaw_cli(
            [
                "--json",
                "worker",
                "spawn",
                run_id,
                "worker-b",
                "--backend",
                "tmux",
                "--task",
                task_b,
                "--skip-permissions",
                "claude",
            ],
            log_dir=log_dir,
            label="spawn-worker-b",
            env=env,
            cwd=repo,
            timeout=120,
        )

        workers = self._wait_for_branchclaw_workers(run_id, env=env, log_dir=log_dir)
        initial_paths = {item["worker_name"]: item["workspace_path"] for item in workers}
        result.notes.append(f"initial_paths={initial_paths}")

        self._wait_for_file(
            Path(initial_paths["worker-a"]) / "tests" / "test_slugify.py",
            timeout=900,
        )
        self._wait_for_file(
            Path(initial_paths["worker-b"]) / "tests" / "test_word_count.py",
            timeout=900,
        )

        self._run_branchclaw_cli(
            ["--json", "worker", "checkpoint", run_id, "worker-a", "--message", "live harness checkpoint a"],
            log_dir=log_dir,
            label="checkpoint-worker-a",
            env=env,
            cwd=repo,
        )
        self._run_branchclaw_cli(
            ["--json", "worker", "checkpoint", run_id, "worker-b", "--message", "live harness checkpoint b"],
            log_dir=log_dir,
            label="checkpoint-worker-b",
            env=env,
            cwd=repo,
        )

        self._run_branchclaw_cli(
            ["--json", "constraint", "add", run_id, "No force pushes; keep functions pure.", "--author", "harness"],
            log_dir=log_dir,
            label="constraint-add",
            env=env,
            cwd=repo,
        )

        blocked = self._run_branchclaw_cli(
            ["archive", "create", run_id, "--label", "phase-1"],
            log_dir=log_dir,
            label="archive-blocked",
            env=env,
            cwd=repo,
            check=False,
        )
        if blocked.returncode == 0:
            raise HarnessError("branchclaw archive unexpectedly succeeded before replan")

        replan = self._run_branchclaw_cli(
            [
                "--json",
                "planner",
                "propose",
                run_id,
                "Continue the same two tasks and respect the new no-force-push pure-function constraint.",
                "--summary",
                "constraint replan",
            ],
            log_dir=log_dir,
            label="replan-propose",
            env=env,
            cwd=repo,
        )
        replan_gate = self._parse_json(replan)["gateId"]
        self._run_branchclaw_cli(
            ["--json", "planner", "approve", run_id, replan_gate, "--actor", "reviewer"],
            log_dir=log_dir,
            label="replan-approve",
            env=env,
            cwd=repo,
        )

        self._run_branchclaw_cli(
            ["--json", "worker", "stop", run_id, "worker-a"],
            log_dir=log_dir,
            label="stop-worker-a",
            env=env,
            cwd=repo,
            timeout=60,
        )
        self._run_branchclaw_cli(
            ["--json", "worker", "stop", run_id, "worker-b"],
            log_dir=log_dir,
            label="stop-worker-b",
            env=env,
            cwd=repo,
            timeout=60,
        )
        self._run_branchclaw_cli(
            ["--json", "worker", "reconcile", run_id],
            log_dir=log_dir,
            label="worker-reconcile",
            env=env,
            cwd=repo,
        )

        archive = self._run_branchclaw_cli(
            ["--json", "archive", "create", run_id, "--label", "phase-1"],
            log_dir=log_dir,
            label="archive-create",
            env=env,
            cwd=repo,
        )
        archive_gate = self._parse_json(archive)["gateId"]
        self._run_branchclaw_cli(
            ["--json", "planner", "approve", run_id, archive_gate, "--actor", "reviewer"],
            log_dir=log_dir,
            label="archive-approve",
            env=env,
            cwd=repo,
        )

        restore = self._run_branchclaw_cli(
            ["--json", "archive", "restore", run_id, self._parse_json(archive)["archiveId"], "--actor", "reviewer"],
            log_dir=log_dir,
            label="archive-restore-request",
            env=env,
            cwd=repo,
        )
        restore_gate = self._parse_json(restore)["id"]
        self._run_branchclaw_cli(
            ["--json", "planner", "approve", run_id, restore_gate, "--actor", "reviewer"],
            log_dir=log_dir,
            label="archive-restore-approve",
            env=env,
            cwd=repo,
        )

        final_state = self._run_branchclaw_cli(
            ["--json", "run", "show", run_id],
            log_dir=log_dir,
            label="run-show-final",
            env=env,
            cwd=repo,
        )
        final_payload = self._parse_json(final_state)
        restored_paths = {item["worker_name"]: item["workspace_path"] for item in final_payload["workers"]}
        if restored_paths["worker-a"] == initial_paths["worker-a"]:
            raise HarnessError("branchclaw restore did not produce a new worker-a workspace path")
        if restored_paths["worker-b"] == initial_paths["worker-b"]:
            raise HarnessError("branchclaw restore did not produce a new worker-b workspace path")

        self._run_branchclaw_cli(
            ["--json", "event", "export", run_id, "--out", str(log_dir / "event-export.json")],
            log_dir=log_dir,
            label="event-export",
            env=env,
            cwd=repo,
        )
        self._run_branchclaw_cli(
            ["--json", "board", "show", run_id],
            log_dir=log_dir,
            label="board-show",
            env=env,
            cwd=repo,
        )
        self._capture_git_snapshot(repo, log_dir / "repo-snapshot")
        for worker_name, workspace_path in initial_paths.items():
            self._capture_git_snapshot(Path(workspace_path), log_dir / f"{worker_name}-workspace")

        result.ok = True
        result.answers = {
            "interactive_spawn_stable": "yes",
            "observability_complete": "yes",
            "daily_loop_still_works": "yes",
        }
        return result

    def _run_clawteam(self) -> ScenarioResult:
        result = ScenarioResult(name="clawteam")
        log_dir = self.artifact_root / "clawteam"
        log_dir.mkdir(parents=True, exist_ok=True)
        root = self.temp_root / "clawteam"
        repo = self._init_seed_repo(root / "repo")
        env = self._clawteam_env(root)
        team_name = "live-legacy"

        self._run_clawteam_cli(
            ["team", "spawn-team", team_name, "-d", "Live Claude acceptance", "-n", "leader"],
            log_dir=log_dir,
            label="team-create",
            env=env,
            cwd=repo,
        )
        self._run_clawteam_cli(
            [
                "--json",
                "task",
                "create",
                team_name,
                "Implement slugify helper",
                "-o",
                "worker1",
                "-d",
                "Implement slugify(value) in slugify.py and create tests/test_slugify.py",
            ],
            log_dir=log_dir,
            label="task-create-worker1",
            env=env,
            cwd=repo,
        )
        self._run_clawteam_cli(
            [
                "--json",
                "task",
                "create",
                team_name,
                "Implement word count helper",
                "-o",
                "worker2",
                "-d",
                "Implement word_count(text) in word_count.py, add tests/test_word_count.py, and update README.md",
            ],
            log_dir=log_dir,
            label="task-create-worker2",
            env=env,
            cwd=repo,
        )

        self._run_clawteam_cli(
            [
                "spawn",
                "tmux",
                "claude",
                "--team",
                team_name,
                "--agent-name",
                "worker1",
                "--task",
                "Implement slugify.py and tests/test_slugify.py. Mark your task completed when done.",
                "--skip-permissions",
            ],
            log_dir=log_dir,
            label="spawn-worker1",
            env=env,
            cwd=repo,
            timeout=120,
        )
        self._run_clawteam_cli(
            [
                "spawn",
                "tmux",
                "claude",
                "--team",
                team_name,
                "--agent-name",
                "worker2",
                "--task",
                "Implement word_count.py, tests/test_word_count.py, and update README.md. Mark your task completed when done.",
                "--skip-permissions",
            ],
            log_dir=log_dir,
            label="spawn-worker2",
            env=env,
            cwd=repo,
            timeout=120,
        )

        self._run_clawteam_cli(
            ["task", "wait", team_name, "--timeout", str(min(self.timeout_seconds, 900)), "--poll-interval", "5"],
            log_dir=log_dir,
            label="task-wait",
            env=env,
            cwd=repo,
            timeout=min(self.timeout_seconds, 900) + 30,
        )
        self._run_clawteam_cli(
            ["board", "show", team_name],
            log_dir=log_dir,
            label="board-show",
            env=env,
            cwd=repo,
        )
        self._run_clawteam_cli(
            ["task", "list", team_name],
            log_dir=log_dir,
            label="task-list",
            env=env,
            cwd=repo,
        )
        self._run_clawteam_cli(
            ["inbox", "receive", team_name],
            log_dir=log_dir,
            label="inbox-receive",
            env=env,
            cwd=repo,
            check=False,
        )

        workspaces_root = Path(env["CLAWTEAM_DATA_DIR"]) / "workspaces" / team_name
        workspace_paths = sorted(path for path in workspaces_root.iterdir() if path.is_dir())
        if len(workspace_paths) < 2:
            raise HarnessError("clawteam did not create two worker workspaces")
        for path in workspace_paths:
            self._capture_git_snapshot(path, log_dir / f"workspace-{path.name}")

        self._capture_tmux_session(f"clawteam-{team_name}", log_dir / "tmux")
        self._cleanup_clawteam_state(repo, env, team_name, log_dir)

        result.ok = True
        result.answers = {
            "interactive_spawn_stable": "yes",
            "observability_complete": "yes",
            "daily_loop_still_works": "yes",
        }
        return result

    def _run(self, args: list[str], *, log_dir: Path, label: str, env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True, timeout: int | float | None = None) -> CommandResult:
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
        cmd_result = CommandResult(
            args=args,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            log_prefix=prefix,
        )
        if check and result.returncode != 0:
            raise HarnessError(
                f"Command failed ({result.returncode}): {command_text}\n{result.stderr or result.stdout}"
            )
        return cmd_result

    def _run_branchclaw_cli(self, args: list[str], *, log_dir: Path, label: str, env: dict[str, str], cwd: Path, check: bool = True, timeout: int | float | None = None) -> CommandResult:
        return self._run(
            [sys.executable, "-m", "branchclaw", *args],
            log_dir=log_dir,
            label=label,
            env=env,
            cwd=cwd,
            check=check,
            timeout=timeout,
        )

    def _run_clawteam_cli(self, args: list[str], *, log_dir: Path, label: str, env: dict[str, str], cwd: Path, check: bool = True, timeout: int | float | None = None) -> CommandResult:
        return self._run(
            [sys.executable, "-m", "clawteam", *args],
            log_dir=log_dir,
            label=label,
            env=env,
            cwd=cwd,
            check=check,
            timeout=timeout,
        )

    def _parse_json(self, result: CommandResult) -> Any:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise HarnessError(f"Expected JSON output from {' '.join(result.args)}") from exc

    def _branchclaw_env(self, root: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["BRANCHCLAW_DATA_DIR"] = str(root / ".branchclaw")
        env["BRANCHCLAW_SKIP_PERMISSIONS"] = "1"
        env["BRANCHCLAW_SUPERVISOR_START_TIMEOUT"] = "45"
        env["BRANCHCLAW_CLAUDE_READY_TIMEOUT"] = "30"
        env["CLAWTEAM_DATA_DIR"] = str(root / ".clawteam")
        return env

    def _clawteam_env(self, root: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["CLAWTEAM_DATA_DIR"] = str(root / ".clawteam")
        env["CLAWTEAM_AGENT_ID"] = "live-leader-001"
        env["CLAWTEAM_AGENT_NAME"] = "leader"
        env["CLAWTEAM_AGENT_TYPE"] = "leader"
        env["BRANCHCLAW_DATA_DIR"] = str(root / ".branchclaw")
        return env

    def _init_seed_repo(self, repo: Path) -> Path:
        repo.mkdir(parents=True, exist_ok=True)
        self._run(["git", "init", "-b", "main"], log_dir=self.artifact_root / "_repo", label=f"git-init-{repo.name}", cwd=repo)
        self._run(["git", "config", "user.email", "live@example.com"], log_dir=self.artifact_root / "_repo", label=f"git-email-{repo.name}", cwd=repo)
        self._run(["git", "config", "user.name", "Live Claude Harness"], log_dir=self.artifact_root / "_repo", label=f"git-name-{repo.name}", cwd=repo)
        (repo / "README.md").write_text(
            "# Live Claude Seed Repo\n\nThis repository is used for live Claude acceptance tests.\n",
            encoding="utf-8",
        )
        (repo / "slugify.py").write_text(
            textwrap.dedent(
                """
                def slugify(value: str) -> str:
                    \"\"\"Return a URL-safe slug for the given value.\"\"\"
                    return value
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (repo / "word_count.py").write_text(
            textwrap.dedent(
                """
                def word_count(text: str) -> int:
                    \"\"\"Return the number of words in text.\"\"\"
                    return 0
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (repo / "tests").mkdir(exist_ok=True)
        self._run(["git", "add", "README.md", "slugify.py", "word_count.py", "tests"], log_dir=self.artifact_root / "_repo", label=f"git-add-{repo.name}", cwd=repo)
        self._run(["git", "commit", "-m", "seed repo"], log_dir=self.artifact_root / "_repo", label=f"git-commit-{repo.name}", cwd=repo)
        return repo

    def _standalone_tmux_claude_smoke(self, log_dir: Path) -> None:
        session_name = "branchclaw-live-preflight"
        standalone_repo = self._init_seed_repo(self.temp_root / "preflight-repo")
        self._cleanup_tmux_sessions(prefix=session_name)
        full_cmd = (
            "unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION 2>/dev/null; "
            f"cd {_shell_quote(str(standalone_repo))} && claude --dangerously-skip-permissions"
        )
        self._run(
            ["tmux", "new-session", "-d", "-s", session_name, "-n", "claude", full_cmd],
            log_dir=log_dir,
            label="standalone-claude-launch",
        )
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            pane = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{session_name}:claude"],
                capture_output=True,
                text=True,
            )
            pane_text = pane.stdout if pane.returncode == 0 else ""
            pane_lower = pane_text.lower()
            if pane_text.strip():
                (log_dir / "standalone-pane.txt").write_text(pane_text, encoding="utf-8")
            if "bypass permissions mode" in pane_lower and "yes, i accept" in pane_lower:
                subprocess.run(["tmux", "send-keys", "-t", f"{session_name}:claude", "Down"])
                time.sleep(0.2)
                subprocess.run(["tmux", "send-keys", "-t", f"{session_name}:claude", "Enter"])
                time.sleep(1.0)
                continue
            if (
                "choose the text style that looks best with your terminal" in pane_lower
                and "dark mode" in pane_lower
                and "light mode" in pane_lower
            ):
                subprocess.run(["tmux", "send-keys", "-t", f"{session_name}:claude", "Enter"])
                time.sleep(1.0)
                continue
            if (
                "browser didn't open? use the url below to sign in" in pane_lower
                or "paste code here if prompted" in pane_lower
                or "oauth/authorize" in pane_lower
            ):
                raise HarnessError("Claude CLI requires interactive sign-in before live acceptance can run")
            if (
                ("trust this folder" in pane_lower or "trust the contents" in pane_lower)
                and ("enter to confirm" in pane_lower or "press enter" in pane_lower or "enter to continue" in pane_lower)
            ):
                subprocess.run(["tmux", "send-keys", "-t", f"{session_name}:claude", "Enter"])
                time.sleep(1.0)
                continue
            lines = [line.strip() for line in pane_text.splitlines() if line.strip()]
            tail = lines[-10:] if len(lines) >= 10 else lines
            if any(line.startswith(("❯", ">", "›")) for line in tail):
                ready = True
                break
            if "Try " in pane_text and "write a test" in pane_text:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise HarnessError("Standalone tmux Claude launch did not reach an interactive ready prompt")
        self._cleanup_tmux_sessions(prefix=session_name)

    def _wait_for_branchclaw_workers(self, run_id: str, *, env: dict[str, str], log_dir: Path) -> list[dict[str, Any]]:
        deadline = time.time() + min(self.timeout_seconds, 180)
        last_payload: dict[str, Any] | None = None
        while time.time() < deadline:
            result = self._run_branchclaw_cli(
                ["--json", "run", "show", run_id],
                log_dir=log_dir,
                label="run-show-poll",
                env=env,
                cwd=self.temp_root / "branchclaw" / "repo",
            )
            payload = self._parse_json(result)
            last_payload = payload
            workers = payload.get("workers", [])
            if len(workers) == 2 and all(item["status"] == "running" for item in workers):
                return workers
            time.sleep(2)
        raise HarnessError(f"BranchClaw workers did not reach running state: {last_payload}")

    def _wait_for_file(self, path: Path, *, timeout: int | float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists() and path.read_text(encoding="utf-8").strip():
                return
            time.sleep(5)
        raise HarnessError(f"Timed out waiting for file: {path}")

    def _capture_git_snapshot(self, repo: Path, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self._run(["git", "status", "--short"], log_dir=log_dir, label="git-status", cwd=repo, check=False)
        self._run(["git", "branch", "--show-current"], log_dir=log_dir, label="git-branch", cwd=repo, check=False)
        self._run(["git", "log", "--oneline", "-5"], log_dir=log_dir, label="git-log", cwd=repo, check=False)

    def _capture_tmux_session(self, session_name: str, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        windows = subprocess.run(
            ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        (log_dir / "windows.txt").write_text(windows.stdout, encoding="utf-8")
        for window_name in [line.strip() for line in windows.stdout.splitlines() if line.strip()]:
            pane = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{session_name}:{window_name}"],
                capture_output=True,
                text=True,
            )
            (log_dir / f"{window_name}.txt").write_text(pane.stdout, encoding="utf-8")

    def _cleanup_tmux_sessions(self, *, prefix: str) -> None:
        sessions = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        if sessions.returncode != 0:
            return
        for name in [line.strip() for line in sessions.stdout.splitlines() if line.strip()]:
            if not name.startswith(prefix):
                continue
            subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, text=True)

    def _cleanup_clawteam_state(self, repo: Path, env: dict[str, str], team_name: str, log_dir: Path) -> None:
        self._capture_tmux_session(f"clawteam-{team_name}", log_dir / "tmux-final")
        self._cleanup_tmux_sessions(prefix=f"clawteam-{team_name}")
        worktree_list = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        for line in worktree_list.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            worktree_path = line.split(" ", 1)[1]
            if "/.clawteam/" not in worktree_path:
                continue
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                cwd=repo,
                capture_output=True,
                text=True,
            )
        branches = subprocess.run(
            ["git", "branch", "--list", "clawteam/*"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        for branch in [item.strip().lstrip("* ").strip() for item in branches.stdout.splitlines() if item.strip()]:
            subprocess.run(["git", "branch", "-D", branch], cwd=repo, capture_output=True, text=True)
        shutil.rmtree(Path(env["CLAWTEAM_DATA_DIR"]), ignore_errors=True)

    def _write_summary(self, *, error: str | None = None) -> None:
        lines = [
            "# Live Claude Acceptance Summary",
            "",
            f"- Generated At: {datetime.now(timezone.utc).isoformat()}",
            f"- Target: {self.target}",
            f"- Artifact Root: {self.artifact_root}",
        ]
        if error:
            lines.extend(["", "## Result", "", f"- FAILED: {error}"])
        else:
            lines.extend(["", "## Result"])
            for result in self.results:
                status = "PASS" if result.ok else "FAIL"
                lines.append(f"- {result.name}: {status}")
                for key, value in result.answers.items():
                    lines.append(f"  - {key}: {value}")
                for note in result.notes:
                    lines.append(f"  - note: {note}")
        (self.artifact_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _shell_quote(value: str) -> str:
    if not value:
        return "''"
    if all(char.isalnum() or char in "-_./:=+" for char in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _print_terminal_summary(summary_path: Path, *, stream: Any = None) -> None:
    output = stream or sys.stdout
    print(f"[live-claude] Summary: {summary_path}", file=output)
    if not summary_path.exists():
        return
    summary = summary_path.read_text(encoding="utf-8")
    marker = "## Result"
    if marker not in summary:
        return
    result_block = summary.split(marker, 1)[1].strip()
    if result_block:
        print(result_block, file=output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=("branchclaw", "clawteam", "both"),
        default="both",
        help="Which live acceptance target to execute.",
    )
    parser.add_argument(
        "--artifact-dir",
        default="",
        help="Override artifact directory (default: artifacts/live-claude/<timestamp>).",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=DEFAULT_TIMEOUT_MINUTES,
        help="Overall timeout budget per scenario.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    artifact_root = (
        Path(args.artifact_dir)
        if args.artifact_dir
        else Path.cwd() / "artifacts" / "live-claude" / timestamp
    )
    harness = LiveClaudeAcceptance(
        target=args.target,
        artifact_root=artifact_root,
        timeout_minutes=args.timeout_minutes,
    )
    try:
        exit_code = harness.run()
    except HarnessError as exc:
        _print_terminal_summary(artifact_root / "summary.md", stream=sys.stderr)
        print(f"[live-claude] {exc}", file=sys.stderr)
        return 1
    _print_terminal_summary(artifact_root / "summary.md")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
