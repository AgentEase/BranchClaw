# BranchClaw Manual Human-in-the-Loop E2E: Three.js Webapp

This runbook executes one full `branchclaw` lifecycle against a real public Three.js / React Three Fiber project while keeping all approval gates in human hands.

Target repository:

- `https://github.com/sanidhyy/threejs-portfolio.git`

This flow does not use `live_claude_acceptance.py` or `manual_threejs_longrun.py`. It is a pure manual operator runbook using the existing CLI.

Raw CLI commands covered by this runbook:

- `branchclaw daemon start`
- `branchclaw run create`
- `branchclaw planner propose`
- `branchclaw planner approve`
- `branchclaw worker spawn`
- `branchclaw archive create`
- `branchclaw archive restore`
- `branchclaw run merge-request`
- `branchclaw board show`
- `branchclaw event export`

## 0. Command Wrapper and Artifact Root

Set one wrapper for `branchclaw` from this repository, then create an isolated artifact root:

```bash
export BRANCHCLAW_ROOT=/path/to/BranchClaw
bc() { uv run --project "$BRANCHCLAW_ROOT" branchclaw "$@"; }

TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
ARTIFACT_ROOT="$BRANCHCLAW_ROOT/artifacts/manual-e2e-threejs/$TIMESTAMP"
TARGET_ROOT="$ARTIFACT_ROOT/workdir"
TARGET_REPO="$TARGET_ROOT/repo"
REVIEWER="${REVIEWER:-manual-reviewer}"

mkdir -p "$ARTIFACT_ROOT"/{reviews,logs,probes,inputs}
```

If you already installed `branchclaw` globally, replace `bc ...` with `branchclaw ...`.

## 1. Clone and Prepare the Real Project

```bash
git clone --depth 1 https://github.com/sanidhyy/threejs-portfolio.git "$TARGET_REPO"
cd "$TARGET_REPO"
git config user.email "manual-e2e@example.com"
git config user.name "BranchClaw Manual E2E"
git checkout -B manual/integration
```

Add a local dummy env file so dev/build can run without real EmailJS credentials:

```bash
cat > .env.local <<'EOF'
VITE_APP_SERVICE_ID=service_dummy_branchclaw
VITE_APP_TEMPLATE_ID=template_dummy_branchclaw
VITE_APP_EMAIL=branchclaw@example.com
VITE_APP_PUBLIC_KEY=public_dummy_branchclaw
EOF
```

Install and capture baseline logs:

```bash
npm install --legacy-peer-deps | tee "$ARTIFACT_ROOT/logs/00-baseline-install.log"
npm run build | tee "$ARTIFACT_ROOT/logs/00-baseline-build.log"
git status --short | tee "$ARTIFACT_ROOT/logs/00-baseline-git-status.txt"
```

## 2. Start BranchClaw and Create the Run

Point BranchClaw state at the isolated artifact root and start the explicit daemon:

```bash
export BRANCHCLAW_DATA_DIR="$ARTIFACT_ROOT/.branchclaw"
bc daemon start
bc daemon status | tee "$ARTIFACT_ROOT/logs/01-daemon-status.txt"
bc daemon ps | tee "$ARTIFACT_ROOT/logs/01-daemon-ps.txt"
```

Write the shared spec, rules, plan, and worker tasks into artifact files:

```bash
cat > "$ARTIFACT_ROOT/inputs/spec.md" <<'EOF'
Run one real BranchClaw manual end-to-end cycle on a Three.js / React Three Fiber portfolio.
Prioritize visible visual improvements, preserve navigation and responsiveness, and keep the
site buildable. Every worker must report a structured result before archive approval.
EOF

cat > "$ARTIFACT_ROOT/inputs/rules.md" <<'EOF'
- Do not break existing navigation, section order, or 3D scene interactivity.
- Preserve responsive behavior for desktop and mobile.
- Worker A uses Vite port 4173.
- Worker B uses Vite port 4174.
- Keep changes reviewable and explain blockers explicitly.
EOF

cat > "$ARTIFACT_ROOT/inputs/plan.md" <<'EOF'
Worker A owns scene visuals: hero scene composition, lighting, materials, camera feel, and 3D atmosphere.
Worker B owns surface polish: typography, spacing, section rhythm, loading states, and mobile presentation.
Both workers must install dependencies in their own worktrees, run the app on their assigned ports,
discover the real preview URL, and publish a structured worker result before stopping.
EOF

cat > "$ARTIFACT_ROOT/inputs/task-worker-a.md" <<'EOF'
Use BranchClaw MCP tools first. Focus on the 3D experience: hero scene composition, lighting,
materials, camera motion, layering, and overall atmosphere. Install dependencies in this worktree,
run the Vite app on port 4173, discover the actual preview URL, and publish a structured worker result.
EOF

cat > "$ARTIFACT_ROOT/inputs/task-worker-b.md" <<'EOF'
Use BranchClaw MCP tools first. Focus on typography, spacing, section rhythm, loading/fallback states,
and mobile-friendly presentation. Install dependencies in this worktree, run the Vite app on port 4174,
discover the actual preview URL, and publish a structured worker result.
EOF
```

Create the run:

```bash
RUN_JSON=$(bc --json run create threejs-manual-e2e \
  --repo "$TARGET_REPO" \
  --project-profile web \
  --spec "$ARTIFACT_ROOT/inputs/spec.md" \
  --rules "$ARTIFACT_ROOT/inputs/rules.md")

RUN_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$RUN_JSON")
echo "$RUN_ID" | tee "$ARTIFACT_ROOT/logs/02-run-id.txt"
bc --json run show "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/02-run-created.json"
```

## 3. Propose the Plan and Manually Approve It

```bash
PLAN_JSON=$(bc --json planner propose "$RUN_ID" "$ARTIFACT_ROOT/inputs/plan.md" \
  --summary "manual threejs e2e initial plan" \
  --author "$REVIEWER")

PLAN_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["gateId"])' <<< "$PLAN_JSON")
bc planner resume "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/03-plan.bundle.md"
bc --json run show "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/03-plan.run.json"
bc board show "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/03-plan.board.txt"
```

Before approving, write a short gate note:

```bash
cat > "$ARTIFACT_ROOT/reviews/03-plan-review.md" <<EOF
# Gate Review: Plan
Run ID: $RUN_ID
Gate ID: $PLAN_GATE
Goal: Approve the initial BranchClaw execution plan for the real Three.js project.
Checks:
- planner bundle reviewed
- pending approval visible in run show / board show
- worker responsibilities look isolated and reviewable
Decision:
Approved by:
Timestamp:
EOF
```

Approve the plan yourself:

```bash
bc planner approve "$RUN_ID" "$PLAN_GATE" --actor "$REVIEWER"
```

## 4. Spawn Workers and Open the Daemon Dashboard

Ask the daemon for the current dashboard URL and open the returned URL in a browser if useful:

```bash
bc board serve | tee "$ARTIFACT_ROOT/logs/04-board-serve.txt"
bc daemon ps | tee "$ARTIFACT_ROOT/logs/04-daemon-ps.txt"
```

Spawn both workers:

```bash
bc --json worker spawn "$RUN_ID" worker-a \
  --backend tmux \
  --task "$(cat "$ARTIFACT_ROOT/inputs/task-worker-a.md")" \
  --skip-permissions \
  claude | tee "$ARTIFACT_ROOT/logs/04-spawn-worker-a.json"

bc --json worker spawn "$RUN_ID" worker-b \
  --backend tmux \
  --task "$(cat "$ARTIFACT_ROOT/inputs/task-worker-b.md")" \
  --skip-permissions \
  claude | tee "$ARTIFACT_ROOT/logs/04-spawn-worker-b.json"
```

Monitor until both workers show previews and reported results:

```bash
bc --json worker list "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/04-workers-running.json"
bc --json run show "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/04-run-running.json"
bc event export "$RUN_ID" --out "$ARTIFACT_ROOT/reviews/04-events-running.json"
bc board show "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/04-board-running.txt"
```

## 5. Capture Preview and Browser Evidence

Read the preview URLs from `04-workers-running.json` or `04-run-running.json`, then set them:

```bash
WORKER_A_URL=http://127.0.0.1:4173/
WORKER_B_URL=http://127.0.0.1:4174/
```

Capture desktop and mobile evidence with the existing Chrome probe script:

```bash
node "$BRANCHCLAW_ROOT/scripts/chrome_probe.mjs" \
  --url "$WORKER_A_URL" \
  --out-dir "$ARTIFACT_ROOT/probes/worker-a" \
  --label worker-a-desktop \
  --preset desktop

node "$BRANCHCLAW_ROOT/scripts/chrome_probe.mjs" \
  --url "$WORKER_A_URL" \
  --out-dir "$ARTIFACT_ROOT/probes/worker-a" \
  --label worker-a-mobile \
  --preset mobile

node "$BRANCHCLAW_ROOT/scripts/chrome_probe.mjs" \
  --url "$WORKER_B_URL" \
  --out-dir "$ARTIFACT_ROOT/probes/worker-b" \
  --label worker-b-desktop \
  --preset desktop

node "$BRANCHCLAW_ROOT/scripts/chrome_probe.mjs" \
  --url "$WORKER_B_URL" \
  --out-dir "$ARTIFACT_ROOT/probes/worker-b" \
  --label worker-b-mobile \
  --preset mobile
```

## 6. Stop Workers and Prepare Archive Approval

Stop and reconcile:

```bash
bc worker stop "$RUN_ID" worker-a
bc worker stop "$RUN_ID" worker-b
bc worker reconcile "$RUN_ID"
bc --json run show "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/05-before-archive.run.json"
bc --json worker list "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/05-before-archive.workers.json"
```

Extract both workspace paths from the saved JSON:

```bash
WORKER_A_WS=$(python3 - "$ARTIFACT_ROOT/reviews/05-before-archive.run.json" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
for worker in payload["workers"]:
    if worker["worker_name"] == "worker-a":
        print(worker["workspace_path"])
        break
PY
)

WORKER_B_WS=$(python3 - "$ARTIFACT_ROOT/reviews/05-before-archive.run.json" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
for worker in payload["workers"]:
    if worker["worker_name"] == "worker-b":
        print(worker["workspace_path"])
        break
PY
)
```

Build each candidate workspace before archive approval:

```bash
(cd "$WORKER_A_WS" && npm run build) | tee "$ARTIFACT_ROOT/logs/05-worker-a-build.log"
(cd "$WORKER_B_WS" && npm run build) | tee "$ARTIFACT_ROOT/logs/05-worker-b-build.log"
```

Create the archive request:

```bash
ARCHIVE_JSON=$(bc --json archive create "$RUN_ID" --label manual-e2e-iter-01 --summary "Manual threejs e2e archive")
ARCHIVE_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["gateId"])' <<< "$ARCHIVE_JSON")
ARCHIVE_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["archiveId"])' <<< "$ARCHIVE_JSON")

bc board show "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/05-before-archive.board.txt"
bc event export "$RUN_ID" --out "$ARTIFACT_ROOT/reviews/05-before-archive.events.json"
```

Write the archive review note:

```bash
cat > "$ARTIFACT_ROOT/reviews/05-archive-review.md" <<EOF
# Gate Review: Archive
Run ID: $RUN_ID
Gate ID: $ARCHIVE_GATE
Archive ID: $ARCHIVE_ID
Checks:
- both workers reported preview/result
- worker workspaces built successfully
- board show and run show reflect worker reports and worktree track
- desktop/mobile probe artifacts captured
Decision:
Approved by:
Timestamp:
EOF
```

Approve the archive yourself:

```bash
bc planner approve "$RUN_ID" "$ARCHIVE_GATE" --actor "$REVIEWER"
bc archive list "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/05-archive-list.txt"
```

## 7. Request Restore and Manually Approve It

Capture pre-restore state:

```bash
bc --json run show "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/06-pre-restore.run.json"
```

Request restore and capture the rollback gate:

```bash
RESTORE_JSON=$(bc --json archive restore "$RUN_ID" "$ARCHIVE_ID" --actor "$REVIEWER")
RESTORE_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$RESTORE_JSON")
```

Write the restore review note:

```bash
cat > "$ARTIFACT_ROOT/reviews/06-restore-review.md" <<EOF
# Gate Review: Restore
Run ID: $RUN_ID
Gate ID: $RESTORE_GATE
Archive ID: $ARCHIVE_ID
Checks:
- target archive is approved
- current workspace paths recorded before restore
- restore is intended only to validate rollback/rebuild semantics
Decision:
Approved by:
Timestamp:
EOF
```

Approve restore yourself:

```bash
bc planner approve "$RUN_ID" "$RESTORE_GATE" --actor "$REVIEWER"
bc --json run show "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/06-post-restore.run.json"
```

Verify that restore created new workspace paths while old ones still exist:

```bash
python3 - "$ARTIFACT_ROOT/reviews/06-pre-restore.run.json" "$ARTIFACT_ROOT/reviews/06-post-restore.run.json" <<'PY'
import json, sys
pre = json.load(open(sys.argv[1], encoding="utf-8"))
post = json.load(open(sys.argv[2], encoding="utf-8"))
def paths(payload):
    return {worker["worker_name"]: worker["workspace_path"] for worker in payload["workers"]}
pre_paths = paths(pre)
post_paths = paths(post)
for name in sorted(pre_paths):
    print(f"{name}: before={pre_paths[name]} after={post_paths[name]}")
PY
```

Then manually confirm both old and new directories exist:

```bash
test -d "$WORKER_A_WS"
test -d "$WORKER_B_WS"
```

## 8. Request Merge and Manually Approve It

Run a clean build on the integration branch before opening the merge gate:

```bash
(cd "$TARGET_REPO" && npm run build) | tee "$ARTIFACT_ROOT/logs/07-pre-merge-build.log"
git -C "$TARGET_REPO" status --short | tee "$ARTIFACT_ROOT/logs/07-pre-merge-git-status.txt"
```

Request merge promotion from the approved archive:

```bash
MERGE_JSON=$(bc --json run merge-request "$RUN_ID" --archive-id "$ARCHIVE_ID" --actor "$REVIEWER")
MERGE_GATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$MERGE_JSON")
```

Write the merge review note:

```bash
cat > "$ARTIFACT_ROOT/reviews/07-merge-review.md" <<EOF
# Gate Review: Merge
Run ID: $RUN_ID
Gate ID: $MERGE_GATE
Archive ID: $ARCHIVE_ID
Checks:
- approved archive selected for promotion
- integration branch is clean and buildable before merge
- restore validation already completed
Decision:
Approved by:
Timestamp:
EOF
```

Approve the merge yourself:

```bash
bc planner approve "$RUN_ID" "$MERGE_GATE" --actor "$REVIEWER"
```

Immediately verify the local integration branch result:

```bash
(cd "$TARGET_REPO" && npm run build) | tee "$ARTIFACT_ROOT/logs/07-post-merge-build.log"
git -C "$TARGET_REPO" status --short | tee "$ARTIFACT_ROOT/logs/07-post-merge-git-status.txt"
git -C "$TARGET_REPO" log --oneline -5 | tee "$ARTIFACT_ROOT/logs/07-post-merge-log.txt"
bc --json run show "$RUN_ID" | tee "$ARTIFACT_ROOT/reviews/07-post-merge.run.json"
bc event export "$RUN_ID" --out "$ARTIFACT_ROOT/reviews/07-post-merge.events.json"
```

## 9. Final Summary and Shutdown

Write the final summary:

```bash
cat > "$ARTIFACT_ROOT/summary.md" <<EOF
# BranchClaw Manual Three.js E2E Summary
Run ID: $RUN_ID
Archive ID: $ARCHIVE_ID
Reviewer: $REVIEWER
Target Repo: https://github.com/sanidhyy/threejs-portfolio.git
Preview URLs:
- worker-a: $WORKER_A_URL
- worker-b: $WORKER_B_URL
Approved Gates:
- plan: $PLAN_GATE
- archive: $ARCHIVE_GATE
- restore: $RESTORE_GATE
- merge: $MERGE_GATE
Artifacts:
- reviews/: manual gate notes and run snapshots
- logs/: build and daemon logs
- probes/: desktop and mobile browser captures
Checks:
- archive carried worker results into worktree track
- restore produced new workspace paths while old ones still existed
- merge completed on the local integration branch
EOF
```

Shut down managed services when you are done:

```bash
bc daemon ps | tee "$ARTIFACT_ROOT/logs/99-daemon-ps-before-stop.txt"
bc daemon stop
```

## Expected Pass Criteria

The run is successful if all of the following are true:

- you manually approved plan, archive, restore, and merge gates
- both workers produced preview URLs and structured results before archive approval
- worker workspaces built before archive approval
- `run show` / `board show` / `event export` captured every stage under `ARTIFACT_ROOT`
- restore produced new workspace paths without deleting the earlier ones
- merge completed locally and the integration branch built after approval
