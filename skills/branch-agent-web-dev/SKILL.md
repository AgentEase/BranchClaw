---
name: branch-agent-web-dev
description: >
  Use when a BranchClaw run is marked as a web-only project and the worker is
  expected to change frontend code, install Node dependencies, launch a dev server
  in tmux, capture a preview URL, and report the changed UI surface.
---

# Branch Agent Web Development

This skill handles pure web projects where the operator expects a visible preview result.
It assumes the shared `branch-agent-project-dev` skill is also present in the prompt.

## Workflow

1. Detect the stack:
   - `python .agents/skills/branch-agent-project-dev/scripts/detect_project.py`
2. Install frontend dependencies:
   - `python .agents/skills/branch-agent-project-dev/scripts/install_deps.py`
3. Start the primary dev server in detached tmux. Prefer the repo's declared dev command,
   for example `npm run dev`, `pnpm dev`, or `yarn dev`.
   - Run the helper from the current project worktree, not by `cd`-ing into the BranchClaw repo.
   - Prefer `--repo-root .` so the helper targets the active worktree explicitly.
4. Capture the preview URL from the tmux log:
   - `python .agents/skills/branch-agent-project-dev/scripts/discover_url.py --log-path <log>`
5. Make the requested UI/code changes.
6. Generate the architecture diff summary.
7. Report the result with:
   - `preview_url`
   - `start_command`
   - `changed_surface_summary`
   - `architecture_summary`

## Output Standard

- If the dev server starts, include the preview URL in the final report.
- If dependencies or the dev server fail, report `blocked` or `failed` explicitly.
- Describe the changed UI surface in operator language, not component-internal jargon only.
