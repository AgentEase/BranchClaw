from __future__ import annotations

import json
import subprocess

from branchclaw.project_tools import (
    build_install_command,
    detect_project_stack,
    discover_urls_from_text,
    generate_architecture_summary,
    install_dependencies,
    launch_tmux_service,
)


def test_detect_project_stack_prefers_declared_package_manager(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "packageManager": "pnpm@9.1.0",
                "scripts": {"dev": "next dev"},
            }
        ),
        encoding="utf-8",
    )
    (repo / "app").mkdir()

    info = detect_project_stack(repo)

    assert info["runtime"] == "node"
    assert info["package_manager"] == "pnpm"
    assert info["frontend"] is True


def test_build_install_command_uses_corepack_for_missing_yarn():
    command = build_install_command(
        ".",
        {"runtime": "node", "package_manager": "yarn"},
        which=lambda _name: None,
    )

    assert command == ["corepack", "yarn", "install"]


def test_discover_urls_from_text_deduplicates_and_strips_punctuation():
    urls = discover_urls_from_text(
        "Ready on http://127.0.0.1:3000, proxy http://127.0.0.1:3000). Backend https://api.local/test;"
    )

    assert urls == ["http://127.0.0.1:3000", "https://api.local/test"]


def test_launch_tmux_service_returns_structured_target(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["tmux", "has-session", "-t"]:
            return Result(returncode=1)
        return Result(returncode=0)

    monkeypatch.setattr("branchclaw.project_tools.shutil.which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr("branchclaw.project_tools.subprocess.run", fake_run)

    result = launch_tmux_service(
        session_name="branchclaw-demo",
        window_name="web",
        cwd=tmp_path,
        command=["npm", "run", "dev"],
        log_path=tmp_path / "dev.log",
        env={"PORT": "3000"},
    )

    assert result["target"] == "branchclaw-demo:web"
    assert "npm run dev" in result["launch_command"]
    assert any(call[:3] == ["tmux", "new-session", "-d"] for call in calls)


def test_generate_architecture_summary_reports_changed_areas(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "api").mkdir()
    (repo / "api" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    (repo / "api" / "app.py").write_text("print('hello world')\n", encoding="utf-8")

    summary = generate_architecture_summary(repo, base_ref="HEAD")

    assert summary.startswith("# Architecture Change Summary")
    assert "`api`" in summary
    assert "`M` `api/app.py`" in summary


def test_install_dependencies_retries_npm_with_legacy_peer_deps(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        calls.append(args)
        if args == ["npm", "install"]:
            return Result(returncode=1, stderr="npm ERR! code ERESOLVE\nunable to resolve dependency tree")
        if args == ["npm", "install", "--legacy-peer-deps"]:
            return Result(returncode=0, stdout="installed")
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr("branchclaw.project_tools.subprocess.run", fake_run)

    result = install_dependencies(
        tmp_path,
        {"runtime": "node", "package_manager": "npm"},
    )

    assert result["ok"] is True
    assert result["command"] == ["npm", "install", "--legacy-peer-deps"]
    assert result["attempted_commands"] == [
        ["npm", "install"],
        ["npm", "install", "--legacy-peer-deps"],
    ]
    assert calls == [
        ["npm", "install"],
        ["npm", "install", "--legacy-peer-deps"],
    ]
