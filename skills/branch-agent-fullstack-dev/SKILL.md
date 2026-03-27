---
name: branch-agent-fullstack-dev
description: >
  Use when a BranchClaw run targets a fullstack project and the worker must handle
  both UI-facing and backend-facing changes. This skill standardizes dependency
  installation, tmux service startup, frontend/backend result capture, and
  cross-layer architecture reporting.
---

# Branch Agent Fullstack Development

This skill is for repos where both the frontend and backend matter to the operator result.
The goal is not just code changes, but a surfaced runtime outcome from both layers.

## Workflow

1. Detect the stack and confirm the repo looks fullstack.
2. Install dependencies before starting services.
3. Start the backend and frontend separately when they are separate commands. Use distinct
   tmux windows and distinct log files.
4. Capture:
   - frontend preview URL
   - backend base URL or a verified endpoint output snippet
5. Apply the requested code changes.
6. Generate an architecture summary from the branch diff.
7. Publish one structured report that includes both layers.

## Output Standard

- `preview_url` should point at the frontend when one exists.
- `backend_url` should point at the backend base URL when available; otherwise use
  `output_snippet` with a verified request/response or startup output.
- `changed_surface_summary` should mention both UI and API/data-flow changes when relevant.
- If only one side can run, report `warning` or `blocked` and explain which side is missing.
