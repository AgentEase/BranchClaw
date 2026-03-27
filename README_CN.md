<h1 align="center"><img src="assets/icon.png" alt="" width="64" style="vertical-align: middle;">&nbsp; BranchClaw：Git 优先的 Agent 工作空间</h1>

<p align="center">
  <strong>从一次性 Worker 到长期软件迭代 🚀<br>
  BranchClaw 把一个仓库变成 planner 驱动的长期 Run、隔离的 Git Worktree、可评审的 Feature，以及批量集成合并流程</strong>
</p>

<p align="center">
  <a href="#-快速开始"><img src="https://img.shields.io/badge/快速开始-5_分钟-blue?style=for-the-badge" alt="Quick Start"></a>
  <a href="#-使用场景"><img src="https://img.shields.io/badge/使用场景-3_个演示-green?style=for-the-badge" alt="Use Cases"></a>
  <a href="#-核心特性"><img src="https://img.shields.io/badge/特性-Worktree_%7C_Feature_%7C_Batch-purple?style=for-the-badge" alt="Features"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/开源协议-MIT-yellow?style=for-the-badge" alt="License"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-≥3.10-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/typer-CLI-green" alt="Typer">
  <img src="https://img.shields.io/badge/agents-Claude_Code_%7C_Codex_%7C_Any_CLI-blueviolet" alt="Agents">
  <img src="https://img.shields.io/badge/runtime-Daemon_%7C_Git_Worktrees_%7C_MCP-orange" alt="Runtime">
  <img src="https://img.shields.io/badge/dashboard-Workspace_%7C_Review_%7C_Control_Plane-0f766e" alt="Dashboard">
</p>

**一个仓库。一个方向。持续迭代。**  
BranchClaw 是这个仓库里的主产品。它为每个仓库和方向维护一个长期 `Run`，让 planner 把方向拆成
`Feature` 队列，把不冲突的工作自动派发到隔离的 Git Worktree，并把 ready 的结果整理成 `Batch`
之后再进入集成分支与 promote 流程。

legacy `clawteam` 仍然保留用于兼容和迁移，但新的工作流应默认从 BranchClaw 开始。  
[**English**](README.md) | [**概念文档**](docs/branchclaw-concepts.md) | [**操作 Runbook**](docs/branchclaw-runbook.md) | [**Three.js 人工 E2E**](docs/branchclaw-manual-e2e-threejs.md)

<p align="center">
  <img src="assets/teaser.png" alt="BranchClaw - Git 优先的 Agent 工作空间" width="800">
</p>

---

<p align="center">
  <video src="https://github.com/user-attachments/assets/7e2f0ecd-8fe3-4970-90ac-5c9669ff060c" controls muted playsinline width="800">
    <a href="https://github.com/user-attachments/assets/7e2f0ecd-8fe3-4970-90ac-5c9669ff060c">观看演示视频</a>
  </video>
</p>
<p align="center">
  <a href="https://github.com/user-attachments/assets/7e2f0ecd-8fe3-4970-90ac-5c9669ff060c">直接打开演示视频</a>
</p>

☝️ 在 BranchClaw 中，操作者给定一个仓库和方向，planner 持续维护 feature backlog，每个 worker
只负责一个 Git worktree，人工只在决策、人工介入和 batch promote 时进入回路。

---

## ✨ 核心特性

<table align="center" width="100%">
<tr>
<td width="25%" align="center" style="vertical-align: top; padding: 15px;">

<h3>🌳 Worktree 优先执行</h3>

<div align="center">
  <img src="https://img.shields.io/badge/Git_Worktrees-Isolated-FF6B6B?style=for-the-badge&logo=git&logoColor=white" alt="Worktrees" />
</div>

<img src="assets/scene-engineering.png" width="180">

<p align="center"><strong>• 一个 worker 对应一个隔离 Git worktree</strong></p>

<p align="center"><strong>• 分支、运行态和结果都保持可评审</strong></p>

<p align="center"><strong>• archive / restore 仍是一等恢复工具</strong></p>

<p align="center"><strong>• worktree graph 是主产品界面</strong></p>

</td>
<td width="25%" align="center" style="vertical-align: top; padding: 15px;">

<h3>📋 Planner 持有 backlog</h3>

<div align="center">
  <img src="https://img.shields.io/badge/Feature_Queue-Backlog-4ECDC4?style=for-the-badge&logo=buffer&logoColor=white" alt="Feature Queue" />
</div>

<img src="assets/scene-template.png" width="180">

<p align="center"><strong>• 一个仓库/方向对应一个长期 Run</strong></p>

<p align="center"><strong>• planner 把计划文本变成显式 FeatureRecord</strong></p>

<p align="center"><strong>• areas / files claim 防止活跃冲突</strong></p>

<p align="center"><strong>• daemon 按并发上限自动派发 feature</strong></p>

</td>
<td width="25%" align="center" style="vertical-align: top; padding: 15px;">

<h3>🧪 Batch 评审与 Promote</h3>

<div align="center">
  <img src="https://img.shields.io/badge/Integration_Branch-Validated-FFD93D?style=for-the-badge&logo=githubactions&logoColor=black" alt="Batch Review" />
</div>

<img src="assets/scene-autoresearch.png" width="180">

<p align="center"><strong>• ready feature 会被组成可评审 batch</strong></p>

<p align="center"><strong>• merge 先进入 <code>integration_ref</code>，不是主分支</strong></p>

<p align="center"><strong>• integration 验证通过后再单独 promote</strong></p>

<p align="center"><strong>• integration 失败会把 feature 退回 ready 并附带 blocker</strong></p>

</td>
<td width="25%" align="center" style="vertical-align: top; padding: 15px;">

<h3>🖥️ Daemon Dashboard</h3>

<div align="center">
  <img src="https://img.shields.io/badge/Workspace_UI-Worktree_First-C77DFF?style=for-the-badge&logo=vercel&logoColor=white" alt="Dashboard" />
</div>

<img src="assets/scene-hedgefund.png" width="180">

<p align="center"><strong>• Picker、Workspace、Review、Control Plane</strong></p>

<p align="center"><strong>• graph 下直接看到 feature queue 和 batch review</strong></p>

<p align="center"><strong>• rescue loop 预算耗尽后进入 intervention queue</strong></p>

<p align="center"><strong>• CLI、SSE 和 dashboard 共用同一份 projection</strong></p>

</td>
</tr>
</table>

---

## 🤔 为什么需要 BranchClaw？

今天很多 Agent 工作流仍然像一次性编排：拉起几个 worker，祈祷它们不要冲突，然后人工收拾残局。

**BranchClaw 改变的是控制单位。**

它不再是“一个提示词、一个 worker、一次合并”，而是：

- 一个长期 `Run` 对应一个仓库和方向
- 一个 `Feature` 对应一个 worker/worktree
- 一个 `Batch` 对应一次评审和集成
- 一个 `integration_ref` 先于主分支

因此你会得到：

- 🚀 **隔离执行**：每个 worker 拥有真实 Git worktree 和分支
- 📋 **planner 持有 backlog**：feature queue、优先级、claim 和 dispatch 全都显式
- 👀 **worktree-first 评审**：dashboard 和 CLI 都围绕同一份 worktree graph
- 🧯 **运行时可靠性**：当 rescue loop 预算耗尽时进入 intervention queue
- 🔄 **更安全的集成**：先合到 integration，再独立 promote

#### ✨ 结果是什么？
你负责设定方向，BranchClaw 负责让 run 持续推进，而你只在 batch 边界做判断。

<p align="center">
  <img src="assets/comic-how-it-works.png" alt="BranchClaw 工作流程：planner、worktree、feature queue、batch review" width="700">
</p>

---

## 🎯 产品循环

<table>
<tr>
<td width="33%">

### 🌱 创建长期 Run
操作者先为一个仓库和一个方向创建长期 Run。Run 会保存共享 spec、rules、集成分支和自动派发并发上限。

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

### 🧠 Planner 把方向变成 Feature
plan approval 不再只是一次性 worker bundle。planner 可以声明显式 feature block，BranchClaw 会把它们转成带 area/file claim 的 backlog 记录。

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

### 📦 评审 Batch，而不是 Worker 混乱
worker 结果会汇入同一份 projection。ready feature 会组成 batch 供评审，先合到 integration 分支，通过验证后再 promote 到主分支。

```bash
branchclaw batch list "$RUN_ID"
branchclaw run merge-request "$RUN_ID" --batch-id <batch-id>
branchclaw run promote-request "$RUN_ID" --batch-id <batch-id>
```

</td>
</tr>
</table>

| | BranchClaw | Legacy ClawTeam | 通用 Agent Runner |
|---|-----------|-----------------|-------------------|
| 🎯 **主对象** | **Run → Worktree → Feature → Batch** | Team → Task → Inbox | Prompt / process |
| ⚡ **执行模型** | 长期 Run + planner backlog | Leader 驱动的 swarm 协作 | 临时拉起 worker |
| 🌳 **隔离** | 真实 Git worktree 与分支 | 真实 Git worktree 与分支 | 不一定 |
| 🧠 **评审单位** | Batch 先入 integration，再 promote | 通常是 task/result 级 | 通常逐 worker |
| 🧯 **恢复方式** | Intervention queue + restart/reconcile/archive | Inbox/task repair | 各工具自定义 |
| 🖥️ **界面** | Worktree-first 的 Agent Workspace | Team/task board | 不一定 |

---

## 🚀 快速开始

安装依赖并启动 daemon：

```bash
uv sync --extra dev
uv run branchclaw daemon start
uv run branchclaw daemon status
```

创建一个长期 Run：

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

提交带显式 feature block 的 backlog：

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

观察 run：

```bash
uv run branchclaw run show "$RUN_ID"
uv run branchclaw feature list "$RUN_ID"
uv run branchclaw batch list "$RUN_ID"
uv run branchclaw worker list "$RUN_ID"
uv run branchclaw board serve
```

评审并 promote：

```bash
uv run branchclaw run merge-request "$RUN_ID" --batch-id <batch-id> --actor reviewer
uv run branchclaw run promote-request "$RUN_ID" --batch-id <batch-id> --actor reviewer
```

更完整的操作说明见：

- [docs/branchclaw-concepts.md](docs/branchclaw-concepts.md)
- [docs/branchclaw-runbook.md](docs/branchclaw-runbook.md)
- [docs/branchclaw-manual-e2e-threejs.md](docs/branchclaw-manual-e2e-threejs.md)

---

## 🎬 使用场景

### 🌐 1. 长期产品仓库

给 BranchClaw 一个产品方向，比如“优化 onboarding”或“继续迭代 landing page”。planner 会维护
feature queue，daemon 会自动派发不冲突的 worktree，而你评审的是 batch，不是逐个 micromanage worker。

### 🎨 2. Web / Full-stack 视觉迭代

对于 web 和 full-stack 仓库，worker 可以使用 project profile、启动真实预览服务、上报结构化 UI 结果，
并把 preview 直接显示在 dashboard review 流中。仓库里的 Three.js manual E2E runbook 就是专门验证这条路径。

### 🧪 3. 人工在环的发布列车

如果你希望关键步骤都由人工审批，可以把 plan、archive、merge-to-integration 和 promote 分成独立 gate。
BranchClaw 负责维持队列推进，而最终判断仍由操作者做出。

---

## 📚 文档

- [docs/branchclaw-concepts.md](docs/branchclaw-concepts.md)：当前产品的核心名词与生命周期
- [docs/branchclaw-runbook.md](docs/branchclaw-runbook.md)：CLI-first 的操作者工作流
- [docs/branchclaw-manual-e2e-threejs.md](docs/branchclaw-manual-e2e-threejs.md)：真实 Three.js 项目的人在环流程
- [docs/index.html](docs/index.html)：文档入口页

legacy `clawteam` 仍为兼容保留。你可以用下面的命令把旧 team 导入 BranchClaw：

```bash
uv run branchclaw run migrate-clawteam <team-name> --repo /path/to/repo
```

如果要跑 live acceptance：

```bash
uv run python scripts/live_claude_acceptance.py --target both
```
