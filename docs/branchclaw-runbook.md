# BranchClaw Operator Runbook

This runbook is for the current `branchclaw` product surface in this repository. It assumes a single-container, Git-first workflow with one planner orchestrating isolated CLI workers.

If you are running directly from the repository, prefix commands with `uv run`. If you installed the package already, use `branchclaw` directly.

## Preconditions

- Python 3.10+
- a Git repository to operate on
- `tmux` if you want interactive worker sessions
- a CLI agent such as `claude`, `codex`, or another supported command

Recommended repo-local setup:

```bash
uv sync --extra dev
uv run branchclaw --help
uv run branchclaw daemon start
```

Installed command map:

- `branchclaw daemon start|stop|status|ps`
- `branchclaw run create`
- `branchclaw planner propose`
- `branchclaw worker spawn`
- `branchclaw constraint add`
- `branchclaw archive create`
- `branchclaw archive restore`
- `branchclaw event export`
- `branchclaw run migrate-clawteam`

## 1. Create a Run

Create the control-plane record, store the shared spec/rules, and pin the target repository:

```bash
RUN_JSON=$(uv run branchclaw --json run create demo \
  --repo . \
  --spec "Ship feature X with reviewable commits" \
  --rules "Keep changes focused and explain blockers early")

RUN_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$RUN_JSON")
echo "$RUN_ID"
```

Inspect the initial projection:

```bash
uv run branchclaw run show "$RUN_ID"
```

## 2. Propose and Approve a Plan

Planner output always goes through a gate:

```bash
PLAN_JSON=$(uv run branchclaw --json planner propose "$RUN_ID" \
  "Worker A implements the API. Worker B adds tests and docs." \
  --summary "phase 1")

PLAN_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["gateId"])' <<< "$PLAN_JSON")
uv run branchclaw planner approve "$RUN_ID" "$PLAN_GATE" --actor reviewer
```

Recompile the current execution bundle at any time:

```bash
uv run branchclaw planner resume "$RUN_ID"
```

## 3. Spawn Workers

Process-managing commands require the explicit global daemon. Start it once per host session:

```bash
uv run branchclaw daemon start
uv run branchclaw daemon status
```

Each worker gets its own Git worktree, branch, runtime status file, and process:

```bash
uv run branchclaw worker spawn "$RUN_ID" worker-a \
  --backend tmux \
  --task "Implement the API" \
  --skip-permissions \
  claude

uv run branchclaw worker spawn "$RUN_ID" worker-b \
  --backend tmux \
  --task "Add tests and update docs" \
  --skip-permissions \
  claude
```

Useful runtime inspection commands:

```bash
uv run branchclaw worker list "$RUN_ID"
uv run branchclaw run show "$RUN_ID"
uv run branchclaw board show "$RUN_ID"
```

## 4. Observe the Run

Terminal view:

```bash
uv run branchclaw board show "$RUN_ID"
```

Web board:

```bash
uv run branchclaw board serve
```

`board serve` now returns the daemon-resident dashboard URL. The dashboard exposes:

- `/api/runs` for run selection
- `/api/daemon/status` for daemon health and dashboard bind info
- `/api/processes` for managed process state
- `/api/data-dirs` for tracked data-dir summaries
- `/api/data-dirs/<dataDirKey>/runs/<run-id>` for the scoped projection snapshot
- `/api/data-dirs/<dataDirKey>/events/<run-id>` for SSE updates

Event inspection:

```bash
uv run branchclaw event tail "$RUN_ID" --follow
uv run branchclaw event export "$RUN_ID" --out branchclaw-run.json
```

Managed process inspection:

```bash
uv run branchclaw daemon ps
```

## 5. Add Constraints and Handle Dirty Replan

Constraints are append-only patches. Adding one marks the run dirty and blocks forward gated actions until a new plan is approved.

```bash
uv run branchclaw constraint add "$RUN_ID" "Do not force-push worker branches"
uv run branchclaw planner resume "$RUN_ID"

REPLAN_JSON=$(uv run branchclaw --json planner propose "$RUN_ID" \
  "Updated plan after the new constraint" \
  --summary "constraint replan")

REPLAN_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["gateId"])' <<< "$REPLAN_JSON")
uv run branchclaw planner approve "$RUN_ID" "$REPLAN_GATE" --actor reviewer
```

## 6. Stop and Reconcile Workers

Before archive/merge/restore transitions, stop live workers and reconcile runtime state:

```bash
uv run branchclaw worker stop "$RUN_ID" worker-a
uv run branchclaw worker stop "$RUN_ID" worker-b
uv run branchclaw worker reconcile "$RUN_ID"
```

Checkpoint a worker branch before stopping if you want a branch-local commit:

```bash
uv run branchclaw worker checkpoint "$RUN_ID" worker-a --message "checkpoint before archive"
```

## 7. Archive, Restore, and Merge

Create an archive request:

```bash
ARCHIVE_JSON=$(uv run branchclaw --json archive create "$RUN_ID" --label phase-1)
ARCHIVE_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["gateId"])' <<< "$ARCHIVE_JSON")
ARCHIVE_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["archiveId"])' <<< "$ARCHIVE_JSON")

uv run branchclaw planner approve "$RUN_ID" "$ARCHIVE_GATE" --actor reviewer
uv run branchclaw archive list "$RUN_ID"
```

Request a restore:

```bash
RESTORE_JSON=$(uv run branchclaw --json archive restore "$RUN_ID" "$ARCHIVE_ID" --actor reviewer)
RESTORE_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$RESTORE_JSON")
uv run branchclaw planner approve "$RUN_ID" "$RESTORE_GATE" --actor reviewer
```

Request a merge promotion from an approved archive:

```bash
MERGE_JSON=$(uv run branchclaw --json run merge-request "$RUN_ID" --archive-id "$ARCHIVE_ID" --actor reviewer)
MERGE_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$MERGE_JSON")
uv run branchclaw planner approve "$RUN_ID" "$MERGE_GATE" --actor reviewer
```

If a merge conflicts, the run enters `merge_blocked` and stays observable through the same board/event surfaces.

## 8. Import Legacy ClawTeam State

You can import an old team as a new BranchClaw run:

```bash
uv run branchclaw run migrate-clawteam <team-name> --repo /path/to/repo
```

Use this when you want to keep old task/message artifacts but move future work into the BranchClaw control plane.

## 9. Live Claude Acceptance

Run the manual live acceptance harness:

```bash
uv run python scripts/live_claude_acceptance.py --target both
```

Behavior:

- BranchClaw and ClawTeam data live in isolated temporary directories during the run
- the host `HOME` is preserved so Claude Code reuses your existing login and local config
- artifacts are written to `artifacts/live-claude/<timestamp>/`
- the top-level result is written to `artifacts/live-claude/<timestamp>/summary.md`

The harness validates:

- `branchclaw` planner, worker spawn, constraint dirty replan, archive, and restore flow
- legacy `clawteam` interactive spawn, board, inbox/task activity, and worktree isolation

## 10. Manual Three.js Long-Run

For a one-shot manual human-in-the-loop real-project run where you approve every gate yourself,
use:

- [docs/branchclaw-manual-e2e-threejs.md](docs/branchclaw-manual-e2e-threejs.md)

For a real visual-iteration run against a public React Three Fiber portfolio:

```bash
uv run python scripts/manual_threejs_longrun.py --smoke
```

Default target repository:

- `https://github.com/sanidhyy/threejs-portfolio.git`

Default long-run behavior:

- `project_profile=web`
- one long-lived BranchClaw run
- two parallel Claude workers
- fixed Vite ports `4173` and `4174`
- no automatic merge to upstream `main`
- artifacts under `artifacts/manual-threejs/<timestamp>/`

The harness also creates a local `longrun/integration` branch inside the isolated clone so each
approved archive can seed the next iteration without touching the upstream repository.

## Current Product Boundaries

BranchClaw currently focuses on:

- planner-driven runs
- worker runtime isolation
- event projection and board observability
- archive/restore/merge lifecycle

It does not currently replace:

- legacy `clawteam team/task/inbox` collaboration semantics
- multi-host transport or distributed orchestration
- heavy frontend infrastructure for the board
