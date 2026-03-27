---
name: branch-agent-project-dev
description: >
  Use when a BranchClaw worker is developing inside an isolated project branch and
  needs a deterministic project-type workflow. This skill standardizes stack
  detection, dependency installation, service startup in tmux, preview/output
  capture, architecture-diff generation, and structured result reporting back into
  BranchClaw.
---

# Branch Agent Project Development Pack

This skill is the shared operating contract for project-branch workers. It is written
to work for Claude, Codex, or any other CLI agent. When BranchClaw MCP tools are
available in the session, they are the primary execution surface. The repo-local helper
scripts in this directory remain the compatibility fallback.

## Use This Skill When

- A BranchClaw run has a `project_profile` and the worker is expected to make code changes.
- The worker must leave behind a runnable preview URL, backend output, or explicit blocker.
- The operator needs structured runtime results in `branchclaw run show`, `worker list`,
  `board show`, and `board serve`.

## Shared Workflow

1. Confirm branch context from `BRANCHCLAW_RUN_ID`, `BRANCHCLAW_WORKER_NAME`, and
   `BRANCHCLAW_PROJECT_PROFILE`.
2. Detect the repo stack before making assumptions:
   - `python .agents/skills/branch-agent-project-dev/scripts/detect_project.py`
3. If MCP tools are available, call them instead of guessing the sequence:
   - `context.get_worker_context`
   - `project.detect`
   - `project.install_dependencies`
   - `service.start_tmux`
   - `service.discover_url`
   - `diff.generate_architecture_summary`
   - `worker.create_checkpoint`
   - `worker.report_result`
   - These are native session tools. Do not try to run them through Bash as `mcp call ...`.
4. If MCP is unavailable, use the helper scripts:
   - `python .agents/skills/branch-agent-project-dev/scripts/install_deps.py`
   - `python .agents/skills/branch-agent-project-dev/scripts/architecture_diff.py`
   - `python .agents/skills/branch-agent-project-dev/scripts/report_result.py ...`
5. Follow the injected profile-specific skill for `web`, `fullstack`, or `backend`.

## Reporting Contract

Every worker must publish one final structured report. Use:

- `status`: `success`, `warning`, `blocked`, or `failed`
- `stack` and `runtime`: detected from the repo, not guessed
- `package_manager`, `install_command`, `start_command`: the exact commands actually used
- `preview_url` for web UI work, `backend_url` or `output_snippet` for backend work
- `changed_surface_summary`: what changed from the user/operator perspective
- `architecture_summary`: Markdown produced from the diff helper
- `warnings` / `blockers`: explicit strings, not hidden in prose

If the project is unsupported or the runtime is missing, report `status=blocked` and include
the blocker instead of pretending the repo was validated.

## Helper Scripts

- `scripts/detect_project.py`: inspect manifests and print structured stack data
- `scripts/install_deps.py`: pick the install command deterministically and run it
- `scripts/start_tmux_service.py`: start a detached local service under tmux and log output
- `scripts/discover_url.py`: wait for a local URL to appear in the captured service log
- `scripts/architecture_diff.py`: generate Markdown summarizing the current branch diff
- `scripts/report_result.py`: publish the structured result back into BranchClaw

Load the reference files in `references/` only when you need detail. The fast path is:
detect → install → run the profile workflow → report.
