# Branch Agent Reporting Contract

Workers publish results into BranchClaw projection, not just final chat prose.

## Minimum Required Fields

- `status`
- `stack`
- `runtime`
- `architecture_summary`

## Add These When Available

- `package_manager`
- `install_command`
- `start_command`
- `preview_url`
- `backend_url`
- `output_snippet`
- `changed_surface_summary`
- `warnings`
- `blockers`

## Status Rules

- `success`: requested development work completed and runtime result captured
- `warning`: work completed but validation/runtime result is partial or degraded
- `blocked`: cannot continue because runtime/tooling/context is missing
- `failed`: attempted execution ended in an error state

## Operator Expectations

- Do not say “works” without a URL, output snippet, or explicit validation command result.
- Do not hide runtime gaps. Missing package manager, missing interpreter, or unsupported stack
  should become a visible blocker in the report.
- `architecture_summary` should come from the helper diff script, not improvised memory.
