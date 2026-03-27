# BranchClaw Concepts

BranchClaw is a Git-first agent workspace. Its main control loop is:

`Workdir -> Run -> Worktree -> Feature -> Batch -> integration_ref -> Promote`

This page defines the current nouns and how they fit together.

## 1. Workdir

`Workdir` is the tracked `.branchclaw` home for one repository context.

It stores:

- run projections and events
- worktree metadata
- daemon/runtime status
- archives, feature backlog, and batch state

In the dashboard, `Workdir` is the entry point on the Picker page.

## 2. Run

A `Run` is the long-lived control record for one repository and one current direction.

Important run fields:

- `repo`: target repository path
- `direction`: high-level product or engineering direction
- `integration_ref`: integration branch used before promote
- `max_active_features`: automatic feature concurrency limit
- `dispatch_mode`: whether planner backlog dispatch is automatic

A run is not a one-shot execution bundle anymore. It is the long-lived envelope that keeps the
planner, workers, feature backlog, batches, interventions, and review history together.

## 3. Worktree

A `Worktree` is the primary execution and review surface.

Each active worker gets:

- one isolated Git worktree
- one branch
- one runtime record
- one result/report path

The dashboard centers on the worktree graph. Archive and restore are also tracked as worktree
lineage states.

## 4. Feature

A `Feature` is the planner-owned backlog item that maps to one worker/worktree.

Feature records carry:

- title and goal
- task summary
- status
- claimed areas
- optional claimed files
- priority
- linked worker name
- linked archive/result summary
- validation status

Typical statuses:

- `queued`
- `assigned`
- `in_progress`
- `ready`
- `batched`
- `merged`
- `blocked`
- `dropped`

Planner proposals can declare feature blocks directly. Example:

```text
## Feature: Hero Polish
Goal: Improve the homepage hero treatment.
Task: Update hero copy and layout.
Areas: ui, hero
Priority: 10
```

The daemon can auto-dispatch features while preventing active conflicts through area/file claims.

## 5. Batch

A `Batch` is the review and merge unit.

BranchClaw does not treat a single worker result as the final merge boundary by default. Instead,
multiple ready features can be grouped into one batch.

Batch records track:

- feature ids
- integration branch target
- batch status
- validation result
- review and approval timestamps

Typical statuses:

- `draft`
- `pending_approval`
- `integrating`
- `integration_failed`
- `pending_promote`
- `completed`
- `rejected`

## 6. integration_ref and Promote

`integration_ref` is the staging branch for batch merges.

Default merge flow:

1. ready features form a batch
2. operator approves merge request
3. batch merges into `integration_ref`
4. host-side integration validation runs
5. if validation passes, a separate promote request can move `integration_ref` to the main branch

This means BranchClaw separates:

- feature completion
- batch review
- integration validation
- final promotion

## 7. Archive and Restore

`Archive` is still first-class, but it is no longer the default product merge unit.

Archive now mainly serves as:

- worktree recovery checkpoint
- feature result snapshot
- restore source for re-opening prior work

`Restore` recreates the worktree state from an archive so the operator or planner can continue work
without rebuilding context manually.

## 8. Decisions and Interventions

BranchClaw has two kinds of human involvement:

### Decisions

Decisions are approval gates such as:

- plan approval
- archive approval
- batch merge approval
- promote approval

### Interventions

Interventions are runtime reliability handoffs.

If rescue loops exhaust their retry/remediation budget, the daemon opens an intervention with:

- reason
- recommended next action
- last failing tool
- remediation history
- related worktree

This keeps runtime recovery separate from product approval.

## 9. Dashboard Pages

The current dashboard is organized into four views:

## Picker

- choose a `Workdir`
- open an existing `Run`
- create a new run

## Workspace

- worktree graph first
- feature queue
- batch review
- needs-attention strip

## Review

- one selected worktree
- summary
- evidence
- pending decisions
- lower tabs for activity, archives, events, and run details

## Control Plane

- daemon health
- tracked workdirs
- managed processes
- daemon-wide interventions

## 10. Compatibility with ClawTeam

This repository still ships legacy `clawteam` for compatibility and migration, but BranchClaw is the
default product direction.

Use legacy import when needed:

```bash
uv run branchclaw run migrate-clawteam <team-name> --repo /path/to/repo
```

That preserves old team/task artifacts while moving future work into the BranchClaw run model.
