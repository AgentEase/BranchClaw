"""Project profile helpers and repo-local skill injection."""

from __future__ import annotations

from pathlib import Path

from branchclaw.models import ProjectProfile

_UMBRELLA_SKILL = "branch-agent-project-dev"
_PROFILE_SKILLS = {
    ProjectProfile.web: "branch-agent-web-dev",
    ProjectProfile.fullstack: "branch-agent-fullstack-dev",
    ProjectProfile.backend: "branch-agent-backend-dev",
}
_HELPER_SCRIPTS = {
    "detect_project": "detect_project.py",
    "install_deps": "install_deps.py",
    "start_tmux_service": "start_tmux_service.py",
    "discover_url": "discover_url.py",
    "report_result": "report_result.py",
    "architecture_diff": "architecture_diff.py",
}
_MCP_TOOLS = [
    "context.get_worker_context",
    "project.detect",
    "project.install_dependencies",
    "service.start_tmux",
    "service.discover_url",
    "service.stop_tmux",
    "diff.generate_architecture_summary",
    "worker.create_checkpoint",
    "worker.report_result",
]


def normalize_project_profile(value: str | ProjectProfile | None) -> ProjectProfile:
    if isinstance(value, ProjectProfile):
        return value
    raw = (value or ProjectProfile.backend.value).strip().lower()
    try:
        return ProjectProfile(raw)
    except ValueError as exc:
        allowed = ", ".join(profile.value for profile in ProjectProfile)
        raise ValueError(f"Invalid project profile '{value}'. Expected one of: {allowed}") from exc


def skills_root() -> Path:
    return Path(__file__).resolve().parents[1] / ".agents" / "skills"


def skill_dir(skill_name: str) -> Path:
    return skills_root() / skill_name


def skill_file(skill_name: str) -> Path:
    return skill_dir(skill_name) / "SKILL.md"


def profile_skill_name(profile: str | ProjectProfile) -> str:
    return _PROFILE_SKILLS[normalize_project_profile(profile)]


def helper_script_paths() -> dict[str, Path]:
    base = skill_dir(_UMBRELLA_SKILL) / "scripts"
    return {name: base / filename for name, filename in _HELPER_SCRIPTS.items()}


def skill_reference_paths(profile: str | ProjectProfile) -> list[Path]:
    selected = profile_skill_name(profile)
    refs: list[Path] = []
    for root in (skill_dir(_UMBRELLA_SKILL) / "references", skill_dir(selected) / "references"):
        if not root.exists():
            continue
        refs.extend(sorted(root.glob("*.md")))
    return refs


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text.strip()
    _, _, remainder = text.partition("\n---\n")
    return remainder.strip()


def read_skill_body(skill_name: str) -> str:
    path = skill_file(skill_name)
    if not path.exists():
        return f"(missing skill file: {path})"
    return _strip_frontmatter(path.read_text(encoding="utf-8"))


def render_project_skill_prompt(
    profile: str | ProjectProfile,
    *,
    mcp_enabled: bool = False,
) -> str:
    normalized = normalize_project_profile(profile)
    selected = profile_skill_name(normalized)
    scripts = helper_script_paths()
    references = skill_reference_paths(normalized)

    lines = [
        "# Project-Type Development Pack",
        f"- Project Profile: {normalized.value}",
        f"- Umbrella Skill: {skill_file(_UMBRELLA_SKILL)}",
        f"- Profile Skill: {skill_file(selected)}",
        "",
    ]
    if mcp_enabled:
        lines.extend(["## BranchClaw MCP Tools"])
        for tool_name in _MCP_TOOLS:
            lines.append(f"- {tool_name}")
        lines.extend(
            [
                "",
                "Use the MCP tools as the primary execution path for project/runtime work.",
                "These are native tools already attached to the current agent session.",
                "Do not try to invoke them through shell commands such as `mcp call ...` or `branchclaw ...`.",
                "Prefer calling tools over manually guessing the sequence of install/start/report steps.",
                "Only fall back to helper scripts if the MCP tool surface is unavailable or a tool cannot express the needed action.",
                "",
            ]
        )
    lines.append("## Helper Scripts")
    for name, path in scripts.items():
        lines.append(f"- {name}: {path}")

    if references:
        lines.extend(["", "## Reference Files"])
        for path in references:
            lines.append(f"- {path}")

    lines.extend(
        [
            "",
            "## Operational Notes",
            "The current working directory is already the isolated project worktree. Do not `cd` into the BranchClaw repository before using helper scripts.",
            "When a helper script supports `--repo-root`, pass `--repo-root .` unless you have a concrete reason to target another directory.",
            "If MCP tools are available in this session, call `context.get_worker_context` first to confirm your task and state before taking action.",
            "For detached web services, prefer this exact tmux helper shape:",
            f"`python {scripts['start_tmux_service']} --repo-root . --session web-preview --window app --log-path .branchclaw-preview.log -- npm run dev -- --host 127.0.0.1 --port <port>`",
            f"Then discover the actual preview URL with `python {scripts['discover_url']} --log-path .branchclaw-preview.log` and report that URL even if the requested port was already occupied.",
            "",
            "## Shared Skill Instructions",
            read_skill_body(_UMBRELLA_SKILL),
            "",
            f"## {normalized.value.title()} Skill Instructions",
            read_skill_body(selected),
            "",
            "## Reporting Contract",
            "Always publish a structured worker result before you stop work.",
            (
                "Prefer the `worker.report_result` MCP tool when it is available in the session."
                if mcp_enabled
                else f"Prefer the helper script at {scripts['report_result']}."
            ),
            (
                "The helper script reads BRANCHCLAW_RUN_ID and BRANCHCLAW_WORKER_NAME from the environment, then calls `branchclaw worker report` for you."
                if mcp_enabled
                else "It reads BRANCHCLAW_RUN_ID and BRANCHCLAW_WORKER_NAME from the environment, then calls `branchclaw worker report` for you."
            ),
            "The result should include, when available: detected stack/runtime, install/start commands used, preview URL or backend output, changed UI/backend surface summary, architecture change markdown, and any warnings or blockers.",
        ]
    )
    return "\n".join(lines).strip()
