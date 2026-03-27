---
name: branch-agent-backend-dev
description: >
  Use when a BranchClaw run targets a backend-only project and the worker must install
  dependencies, run the requested backend service or validation command, capture the
  relevant output, and report the architecture change summary.
---

# Branch Agent Backend Development

This skill is for backend-oriented repos where the operator primarily needs command output,
service output, or endpoint verification rather than a browser preview.

## Workflow

1. Detect the stack and runtime.
2. Install backend dependencies using the shared helper.
3. Run the requested validation command or backend service command.
4. Capture the output the operator will care about:
   - service startup line
   - test output
   - one request/response snippet
   - migration/log output
5. Generate the architecture diff summary.
6. Publish a structured report before stopping.

## Output Standard

- Use `backend_url` when a long-running backend service is available.
- Otherwise use `output_snippet` to show the relevant validation result.
- Always include `architecture_summary`.
- If the runtime is unsupported or missing, report `blocked` instead of guessing.
