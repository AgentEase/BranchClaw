from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_branchclaw_readme_promotes_branchclaw_as_default():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.startswith("# BranchClaw")
    assert "branchclaw run create" in readme
    assert "branchclaw daemon start" in readme
    assert "branchclaw board serve" in readme
    assert "scripts/live_claude_acceptance.py --target both" in readme
    assert "docs/branchclaw-manual-e2e-threejs.md" in readme
    assert "clawteam" in readme.lower()
    assert "legacy" in readme.lower()


def test_branchclaw_chinese_readme_promotes_branchclaw_as_default():
    readme = (ROOT / "README_CN.md").read_text(encoding="utf-8")

    assert readme.startswith("# BranchClaw")
    assert "branchclaw run create" in readme
    assert "branchclaw daemon start" in readme
    assert "branchclaw board serve" in readme
    assert "scripts/live_claude_acceptance.py --target both" in readme
    assert "docs/branchclaw-manual-e2e-threejs.md" in readme
    assert "兼容" in readme


def test_branchclaw_runbook_covers_operator_flow():
    runbook = (ROOT / "docs" / "branchclaw-runbook.md").read_text(encoding="utf-8")

    for needle in [
        "branchclaw run create",
        "branchclaw daemon start",
        "branchclaw planner propose",
        "branchclaw worker spawn",
        "branchclaw constraint add",
        "branchclaw archive create",
        "branchclaw archive restore",
        "branchclaw event export",
        "branchclaw run migrate-clawteam",
        "scripts/live_claude_acceptance.py --target both",
        "docs/branchclaw-manual-e2e-threejs.md",
    ]:
        assert needle in runbook


def test_branchclaw_manual_threejs_e2e_runbook_covers_full_human_flow():
    runbook = (ROOT / "docs" / "branchclaw-manual-e2e-threejs.md").read_text(encoding="utf-8")

    for needle in [
        "https://github.com/sanidhyy/threejs-portfolio.git",
        "branchclaw daemon start",
        "branchclaw run create",
        "branchclaw planner propose",
        "branchclaw planner approve",
        "branchclaw worker spawn",
        "branchclaw archive create",
        "branchclaw archive restore",
        "branchclaw run merge-request",
        "scripts/chrome_probe.mjs",
        "artifacts/manual-e2e-threejs",
    ]:
        assert needle in runbook


def test_branch_agent_skill_pack_is_present_and_not_template_text():
    skill_paths = [
        ROOT / ".agents" / "skills" / "branch-agent-project-dev" / "SKILL.md",
        ROOT / ".agents" / "skills" / "branch-agent-web-dev" / "SKILL.md",
        ROOT / ".agents" / "skills" / "branch-agent-fullstack-dev" / "SKILL.md",
        ROOT / ".agents" / "skills" / "branch-agent-backend-dev" / "SKILL.md",
    ]

    for path in skill_paths:
        text = path.read_text(encoding="utf-8")
        assert "[TODO" not in text
        assert "Use when" in text

    for name in [
        "detect_project.py",
        "install_deps.py",
        "start_tmux_service.py",
        "discover_url.py",
        "report_result.py",
        "architecture_diff.py",
    ]:
        assert (ROOT / ".agents" / "skills" / "branch-agent-project-dev" / "scripts" / name).exists()
