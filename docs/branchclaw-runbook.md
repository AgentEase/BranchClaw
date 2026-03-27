# BranchClaw Operator Runbook

This runbook describes the current BranchClaw product model:

- one repository
- one long-lived run
- one planner-owned feature backlog
- one worktree per active worker
- batch review before merge
- integration branch before promote

If you are running directly from this repository, prefix commands with `uv run`. If `branchclaw`
is already installed globally, you can omit `uv run`.

## Preconditions

- Python 3.10+
- a Git repository to operate on
- `tmux` for interactive worker sessions
- a CLI agent such as `claude`, `codex`, or another supported command

Recommended repo-local setup:

```bash
uv sync --extra dev
uv run branchclaw --help
uv run branchclaw daemon start
```

Useful command families:

- `branchclaw daemon start|stop|status|ps`
- `branchclaw run create|show|list|merge-request|promote-request|migrate-clawteam`
- `branchclaw planner propose|approve|reject|resume`
- `branchclaw worker spawn|list|checkpoint|stop|restart|report|reconcile`
- `branchclaw feature list|show`
- `branchclaw batch list|show`
- `branchclaw archive create|list`
- `branchclaw archive restore`
- `branchclaw constraint add`
- `branchclaw event export|tail`
- `branchclaw board show|serve`

## 1. Start the Daemon

Process-managing commands require the explicit global daemon:

```bash
uv run branchclaw daemon start
uv run branchclaw daemon status
uv run branchclaw daemon ps
```

## 2. Create a Long-Lived Run

Create one run for one repository and one current direction:

```bash
RUN_JSON=$(uv run branchclaw --json run create demo \
  --repo . \
  --project-profile web \
  --direction "Improve onboarding and keep shipping reviewable batches" \
  --integration-ref branchclaw/demo/integration \
  --max-active-features 2 \
  --spec "Continuously ship isolated, reviewable improvements." \
  --rules "Keep the build healthy, keep claims explicit, and report blockers early.")

RUN_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$RUN_JSON")
echo "$RUN_ID"
```

Inspect the initial projection:

```bash
uv run branchclaw run show "$RUN_ID"
```

## 3. Propose a Backlog and Approve It

Planner approval now defines direction and current backlog, not just a one-shot execution bundle.

Example with explicit feature blocks:

```bash
PLAN_JSON=$(uv run branchclaw --json planner propose "$RUN_ID" "$(cat <<'EOF'
## Feature: Hero Polish
Goal: Improve the homepage hero treatment.
Task: Update hero copy and layout.
Areas: ui, hero
Priority: 10

## Feature: API Health
Goal: Add a backend health endpoint.
Task: Implement a lightweight health check.
Areas: api
Priority: 20
EOF
)" --summary "initial backlog" --author planner)

PLAN_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["gateId"])' <<< "$PLAN_JSON")
uv run branchclaw planner approve "$RUN_ID" "$PLAN_GATE" --actor reviewer
```

Recompile the active bundle at any time:

```bash
uv run branchclaw planner resume "$RUN_ID"
```

## 4. Observe Planner Dispatch

After plan approval, the daemon can auto-dispatch non-conflicting features up to the configured
concurrency limit.

Inspect the current state:

```bash
uv run branchclaw run show "$RUN_ID"
uv run branchclaw feature list "$RUN_ID"
uv run branchclaw batch list "$RUN_ID"
uv run branchclaw worker list "$RUN_ID"
uv run branchclaw board show "$RUN_ID"
```

Open the daemon-resident dashboard:

```bash
uv run branchclaw board serve
```

The dashboard is now organized around:

- `Picker`
- `Workspace`
- `Review`
- `Control Plane`

## 5. Manual Worker Override

Automatic dispatch is the default, but you can still manually create a worktree/worker.

Spawn a worker without binding to a feature:

```bash
uv run branchclaw worker spawn "$RUN_ID" worker-a \
  --backend tmux \
  --task "Investigate the onboarding flow" \
  --skip-permissions \
  claude
```

Or override a specific feature:

```bash
uv run branchclaw worker spawn "$RUN_ID" worker-a \
  --feature-id feature-001 \
  --backend tmux \
  --task "Implement the planned hero polish" \
  --skip-permissions \
  claude
```

Checkpoint a branch-local commit when useful:

```bash
uv run branchclaw worker checkpoint "$RUN_ID" worker-a --message "checkpoint before review"
```

## 6. Constraints and Dirty Replan

Constraints are append-only patches. Adding one marks the run dirty and blocks forward gated
actions until a fresh plan is approved.

```bash
uv run branchclaw constraint add "$RUN_ID" "Do not change the signup copy without design review"
uv run branchclaw planner resume "$RUN_ID"

REPLAN_JSON=$(uv run branchclaw --json planner propose "$RUN_ID" \
  "Updated backlog after the new constraint" \
  --summary "constraint replan")

REPLAN_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["gateId"])' <<< "$REPLAN_JSON")
uv run branchclaw planner approve "$RUN_ID" "$REPLAN_GATE" --actor reviewer
```

## 7. Runtime Recovery, Interventions, and Reconcile

BranchClaw has a rescue loop. When retries and auto-remediation are exhausted, it opens an
intervention instead of silently leaving a broken worker running.

Useful commands:

```bash
uv run branchclaw worker list "$RUN_ID"
uv run branchclaw worker reconcile "$RUN_ID"
uv run branchclaw worker restart "$RUN_ID" worker-a
uv run branchclaw event tail "$RUN_ID" --follow
```

Export the event stream without heartbeat noise by default:

```bash
uv run branchclaw event export "$RUN_ID" --out branchclaw-run.json
```

## 8. Review Features and Batches

The product review boundary is now the batch, not the raw worker result.

Inspect current backlog and reviewable batches:

```bash
uv run branchclaw feature list "$RUN_ID"
uv run branchclaw batch list "$RUN_ID"
uv run branchclaw batch show "$RUN_ID" <batch-id>
```

Ready features can be grouped into a batch. Batch merge goes to `integration_ref` first:

```bash
uv run branchclaw run merge-request "$RUN_ID" --batch-id <batch-id> --actor reviewer
```

If integration validation passes, request promote to the main branch:

```bash
uv run branchclaw run promote-request "$RUN_ID" --batch-id <batch-id> --actor reviewer
```

If integration validation fails, BranchClaw keeps the batch out of main and sends the affected
features back to `ready` with blockers.

## 9. Archive and Restore Worktrees

`archive/restore` still matters, but now mainly as worktree recovery and feature snapshot tooling.

Create an archive request:

```bash
ARCHIVE_JSON=$(uv run branchclaw --json archive create "$RUN_ID" --label feature-review-01)
ARCHIVE_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["gateId"])' <<< "$ARCHIVE_JSON")
ARCHIVE_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["archiveId"])' <<< "$ARCHIVE_JSON")

uv run branchclaw planner approve "$RUN_ID" "$ARCHIVE_GATE" --actor reviewer
uv run branchclaw archive list "$RUN_ID"
```

Restore an archive back into a new worktree state:

```bash
RESTORE_JSON=$(uv run branchclaw --json archive restore "$RUN_ID" "$ARCHIVE_ID" --actor reviewer)
RESTORE_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$RESTORE_JSON")
uv run branchclaw planner approve "$RUN_ID" "$RESTORE_GATE" --actor reviewer
```

## 10. Stop Workers Cleanly

Before archive-heavy transitions or manual maintenance, stop and reconcile live workers:

```bash
uv run branchclaw worker stop "$RUN_ID" worker-a
uv run branchclaw worker reconcile "$RUN_ID"
```

Dashboard `Stop` should now also close the worker-owned preview service, not just the runtime
record.

## 11. Migrate Legacy ClawTeam State

You can import an old team into BranchClaw:

```bash
uv run branchclaw run migrate-clawteam <team-name> --repo /path/to/repo
```

Use this when you want to preserve old team/task artifacts but move future work into the current
run/feature/batch model.

## 12. Acceptance and Manual E2E

Live acceptance harness:

```bash
uv run python scripts/live_claude_acceptance.py --target both
```

Manual human-in-the-loop real-project runbook:

- [docs/branchclaw-manual-e2e-threejs.md](docs/branchclaw-manual-e2e-threejs.md)

Smoke long-run against the public Three.js repository:

```bash
uv run python scripts/manual_threejs_longrun.py --smoke
```

Default target repository:

- `https://github.com/sanidhyy/threejs-portfolio.git`
