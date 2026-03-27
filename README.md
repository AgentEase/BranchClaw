<h1 align="center"><img src="assets/icon.png" alt="" width="64" style="vertical-align: middle;">&nbsp; BranchClaw: Git-First Agent Workspace</h1>

<p align="center">
  <strong>From one-off workers to long-lived software iteration 🚀<br>
  BranchClaw turns one repository into a planner-led run, isolated worktrees, reviewable features, and batch-based integration</strong>
</p>

<p align="center">
  <a href="#-quick-start"><img src="https://img.shields.io/badge/Quick_Start-5_min-blue?style=for-the-badge" alt="Quick Start"></a>
  <a href="#-use-cases"><img src="https://img.shields.io/badge/Use_Cases-3_Demos-green?style=for-the-badge" alt="Use Cases"></a>
  <a href="#-key-features"><img src="https://img.shields.io/badge/Features-Worktree_%7C_Feature_%7C_Batch-purple?style=for-the-badge" alt="Features"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge" alt="License"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-≥3.10-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/typer-CLI-green" alt="Typer">
  <img src="https://img.shields.io/badge/agents-Claude_Code_%7C_Codex_%7C_Any_CLI-blueviolet" alt="Agents">
  <img src="https://img.shields.io/badge/runtime-Daemon_%7C_Git_Worktrees_%7C_MCP-orange" alt="Runtime">
  <img src="https://img.shields.io/badge/dashboard-Workspace_%7C_Review_%7C_Control_Plane-0f766e" alt="Dashboard">
</p>

**One repository. One direction. Continuous agent iteration.**  
BranchClaw is the primary product in this repository. It keeps one long-lived `Run` per repo and
direction, lets the planner turn that direction into a feature backlog, dispatches non-conflicting
workers into isolated Git worktrees, and groups ready results into `Batch` review before merge and
promote.

Legacy `clawteam` still ships for compatibility and migration, but new workflows should start with
BranchClaw.  
[**中文文档**](README_CN.md) | [**Concepts**](docs/branchclaw-concepts.md) | [**Operator Runbook**](docs/branchclaw-runbook.md) | [**Manual Three.js E2E**](docs/branchclaw-manual-e2e-threejs.md)

<p align="center">
  <img src="assets/teaser.png" alt="BranchClaw - worktree-first agent workspace" width="800">
</p>

---

<p align="center">
  <video src="https://github.com/user-attachments/assets/7e2f0ecd-8fe3-4970-90ac-5c9669ff060c" controls muted playsinline width="800">
    <a href="https://github.com/user-attachments/assets/7e2f0ecd-8fe3-4970-90ac-5c9669ff060c">Watch the demo video</a>
  </video>
</p>
<p align="center">
  <a href="https://github.com/user-attachments/assets/7e2f0ecd-8fe3-4970-90ac-5c9669ff060c">Open the demo video directly</a>
</p>

☝️ In practice the operator gives BranchClaw a repository and direction, the planner keeps a
backlog of features, each worker owns one Git worktree, and the operator only steps in for
decisions, interventions, and batch promotion.

---

## ✨ Key Features

<table align="center" width="100%">
<tr>
<td width="25%" align="center" style="vertical-align: top; padding: 15px;">

<h3>🌳 Worktree-First Execution</h3>

<div align="center">
  <img src="https://img.shields.io/badge/Git_Worktrees-Isolated-FF6B6B?style=for-the-badge&logo=git&logoColor=white" alt="Worktrees" />
</div>

<img src="assets/scene-engineering.png" width="180">

<p align="center"><strong>• One worker = one isolated Git worktree</strong></p>

<p align="center"><strong>• Branches, runtime state, and results stay reviewable</strong></p>

<p align="center"><strong>• Archive and restore remain first-class recovery tools</strong></p>

<p align="center"><strong>• The worktree graph is the main product surface</strong></p>

</td>
<td width="25%" align="center" style="vertical-align: top; padding: 15px;">

<h3>📋 Planner-Owned Backlog</h3>

<div align="center">
  <img src="https://img.shields.io/badge/Feature_Queue-Backlog-4ECDC4?style=for-the-badge&logo=buffer&logoColor=white" alt="Feature Queue" />
</div>

<img src="assets/scene-template.png" width="180">

<p align="center"><strong>• One long-lived run per repository and direction</strong></p>

<p align="center"><strong>• Planner turns plan text into explicit feature records</strong></p>

<p align="center"><strong>• Claimed areas and files prevent active conflicts</strong></p>

<p align="center"><strong>• Daemon auto-dispatches up to the configured concurrency</strong></p>

</td>
<td width="25%" align="center" style="vertical-align: top; padding: 15px;">

<h3>🧪 Batch Review & Promote</h3>

<div align="center">
  <img src="https://img.shields.io/badge/Integration_Branch-Validated-FFD93D?style=for-the-badge&logo=githubactions&logoColor=black" alt="Batch Review" />
</div>

<img src="assets/scene-autoresearch.png" width="180">

<p align="center"><strong>• Ready features are grouped into reviewable batches</strong></p>

<p align="center"><strong>• Merge goes to <code>integration_ref</code> first, not main</strong></p>

<p align="center"><strong>• Promote is a separate gate after integration validation</strong></p>

<p align="center"><strong>• Failed integration sends features back to ready with blockers</strong></p>

</td>
<td width="25%" align="center" style="vertical-align: top; padding: 15px;">

<h3>🖥️ Daemon Dashboard</h3>

<div align="center">
  <img src="https://img.shields.io/badge/Workspace_UI-Worktree_First-C77DFF?style=for-the-badge&logo=vercel&logoColor=white" alt="Dashboard" />
</div>

<img src="assets/scene-hedgefund.png" width="180">

<p align="center"><strong>• Picker, Workspace, Review, and Control Plane</strong></p>

<p align="center"><strong>• Feature queue and batch review under the graph</strong></p>

<p align="center"><strong>• Intervention queue when runtime recovery needs a human</strong></p>

<p align="center"><strong>• CLI, SSE, and dashboard all read the same projection</strong></p>

</td>
</tr>
</table>

---

## 🤔 Why BranchClaw?

Most agent workflows still feel like one-off orchestration: spawn a few workers, hope they do not
collide, then manually stitch everything back together.

**BranchClaw changes the unit of control.**

Instead of “one prompt, one worker, one merge,” it uses:

- one long-lived `Run` per repository and direction
- one `Feature` per worker/worktree
- one `Batch` per review/merge step
- one `integration_ref` before anything reaches the main branch

That gives you:

- 🚀 **Isolated execution** — every worker owns a real Git worktree and branch
- 📋 **Planner-managed backlog** — feature queue, priority, claims, and dispatch all stay explicit
- 👀 **Worktree-first review** — dashboard and CLI both revolve around the same worktree graph
- 🧯 **Runtime reliability** — interventions open when rescue loops exhaust their budget
- 🔄 **Safer integration** — merge to integration first, then promote separately

#### ✨ The Result?
You set the direction. BranchClaw keeps the run moving, and you review at the batch boundary.

<p align="center">
  <img src="assets/comic-how-it-works.png" alt="How BranchClaw works - planner, worktrees, feature queue, batch review" width="700">
</p>

---

## 🎯 Product Loop in Action

<table>
<tr>
<td width="33%">

### 🌱 Create a Long-Lived Run
The operator creates one run for one repository and one direction. The run stores shared spec,
rules, integration branch, and automatic dispatch limits.

```bash
branchclaw daemon start
branchclaw run create website \
  --repo . \
  --direction "Improve onboarding" \
  --integration-ref branchclaw/website/integration \
  --max-active-features 2
```

</td>
<td width="33%">

### 🧠 Planner Turns Direction into Features
Plan approval no longer means a one-shot worker bundle. The planner can declare explicit
feature blocks, and BranchClaw turns them into backlog records with area/file claims.

```bash
branchclaw planner propose "$RUN_ID" "
## Feature: Hero Polish
Areas: ui, hero
Priority: 10
"
branchclaw planner approve "$RUN_ID" "$PLAN_GATE"
```

</td>
<td width="33%">

### 📦 Review Batches, Not Raw Worker Chaos
Workers report into the same projection. Ready features are batched for review, merged into the
integration branch, validated, then promoted to main only after a second gate.

```bash
branchclaw batch list "$RUN_ID"
branchclaw run merge-request "$RUN_ID" --batch-id <batch-id>
branchclaw run promote-request "$RUN_ID" --batch-id <batch-id>
```

</td>
</tr>
</table>

| | BranchClaw | Legacy ClawTeam | Generic agent runners |
|---|-----------|-----------------|-----------------------|
| 🎯 **Primary object** | **Run → Worktree → Feature → Batch** | Team → Task → Inbox | Prompt / process |
| ⚡ **Execution model** | Long-lived run with planner backlog | Leader-driven swarm collaboration | Ad hoc worker spawning |
| 🌳 **Isolation** | Real Git worktrees and branches | Real Git worktrees and branches | Varies |
| 🧠 **Review unit** | Batch to integration, then promote | Usually task/result level | Usually per worker |
| 🧯 **Recovery** | Intervention queue + restart/reconcile/archive | Inbox/task repair | Tool-specific |
| 🖥️ **UI** | Worktree-first agent workspace | Team/task board | Varies |

---

## 🚀 Quick Start

Install and start the daemon:

```bash
uv sync --extra dev
uv run branchclaw daemon start
uv run branchclaw daemon status
```

Create a run:

```bash
RUN_JSON=$(uv run branchclaw --json run create website \
  --repo . \
  --project-profile web \
  --direction "Improve onboarding and shipping confidence" \
  --integration-ref branchclaw/website/integration \
  --max-active-features 2 \
  --spec "Continuously ship reviewable improvements to the website." \
  --rules "Keep changes isolated, preserve a working build, and report blockers early.")

RUN_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$RUN_JSON")
```

Propose a backlog with explicit feature blocks:

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

Observe the run:

```bash
uv run branchclaw run show "$RUN_ID"
uv run branchclaw feature list "$RUN_ID"
uv run branchclaw batch list "$RUN_ID"
uv run branchclaw worker list "$RUN_ID"
uv run branchclaw board serve
```

Review and promote:

```bash
uv run branchclaw run merge-request "$RUN_ID" --batch-id <batch-id> --actor reviewer
uv run branchclaw run promote-request "$RUN_ID" --batch-id <batch-id> --actor reviewer
```

For a deeper walkthrough, use:

- [docs/branchclaw-concepts.md](docs/branchclaw-concepts.md)
- [docs/branchclaw-runbook.md](docs/branchclaw-runbook.md)
- [docs/branchclaw-manual-e2e-threejs.md](docs/branchclaw-manual-e2e-threejs.md)

---

## 🎬 Use Cases

### 🌐 1. Long-Running Product Repository

Give BranchClaw one product direction such as “improve onboarding” or “ship the next landing-page
iteration.” The planner keeps a feature queue, the daemon dispatches non-conflicting worktrees, and
you review the resulting batch instead of micromanaging each worker.

### 🎨 2. Visual Web Iteration

For web and full-stack repos, workers can use project profiles, start real preview services, report
structured UI results, and surface those previews directly in the dashboard review flow. The
Three.js manual E2E runbook in this repo exists to validate exactly that path.

### 🧪 3. Human-in-the-Loop Release Train

If you want human approval at every important step, keep plan, archive, merge-to-integration, and
promote as separate gates. BranchClaw keeps the queue moving while leaving the final judgment with
the operator.

---

## 📚 Documentation

- [docs/branchclaw-concepts.md](docs/branchclaw-concepts.md): current nouns and lifecycle
- [docs/branchclaw-runbook.md](docs/branchclaw-runbook.md): CLI-first operator workflow
- [docs/branchclaw-manual-e2e-threejs.md](docs/branchclaw-manual-e2e-threejs.md): real-project manual flow
- [docs/index.html](docs/index.html): docs portal

Legacy `clawteam` still ships for compatibility and migration. Import an old team into BranchClaw with:

```bash
uv run branchclaw run migrate-clawteam <team-name> --repo /path/to/repo
```
