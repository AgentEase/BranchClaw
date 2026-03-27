const state = {
  daemon: null,
  dataDirs: [],
  runs: [],
  processes: [],
  selectedDataDirKey: "",
  selectedRunId: "",
  selectedEntryId: "",
  activePage: "picker",
  pendingActions: {},
  snapshot: null,
  snapshotSignature: "",
  recentEvents: [],
  recentEventsSignature: "",
  eventScope: "node",
  eventLevel: "all",
  eventSource: null,
  openDrawer: "",
  expandedGateId: "",
  reviewTab: "activity",
};

const liveStatuses = new Set(["starting", "running", "stale"]);
const pageKinds = ["picker", "workspace", "review", "control-plane"];
const reviewTabs = ["activity", "archives", "events", "run-details"];
const storageKeys = {
  dataDir: "branchclaw:last-data-dir",
  run: "branchclaw:last-run",
};
const pagePartialPaths = {
  picker: "pages/picker.html",
  workspace: "pages/workspace.html",
  review: "pages/review.html",
  "control-plane": "pages/control-plane.html",
};
const pageTemplateCache = new Map();

function serializeState(value) {
  return JSON.stringify(value ?? null);
}

function snapshotComparableValue(data) {
  if (!data || typeof data !== "object") return data;
  const { lastEventAt, ...rest } = data;
  const run = data.run && typeof data.run === "object"
    ? (({ lastEventAt: runLastEventAt, ...stableRun }) => stableRun)(data.run)
    : data.run;
  return {
    ...rest,
    run,
    workers: (data.workers || []).map((worker) => {
      if (!worker || typeof worker !== "object") return worker;
      const {
        heartbeatAgeSeconds,
        heartbeat_at,
        last_heartbeat_at,
        ...stableWorker
      } = worker;
      return stableWorker;
    }),
  };
}

function snapshotSignatureFor(data) {
  return serializeState(snapshotComparableValue(data));
}

function byId(id) {
  return document.getElementById(id);
}

function bind(id, eventName, handler) {
  const element = byId(id);
  if (element) element.addEventListener(eventName, handler);
}

function currentPageId() {
  const fromData = document.body?.dataset.page || "";
  if (pageKinds.includes(fromData)) return fromData;
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  if (path === "/" || path === "/index.html") return "picker";
  if (path === "/workspace.html") return "workspace";
  if (path === "/review.html") return "review";
  if (path === "/control-plane.html") return "control-plane";
  return "picker";
}

function pagePath(page) {
  if (page === "picker") return "/";
  if (page === "workspace") return "/workspace.html";
  if (page === "review") return "/review.html";
  if (page === "control-plane") return "/control-plane.html";
  return "/";
}

function mainPagesContainer() {
  return document.querySelector(".main-pages");
}

function templateMarkupFromDom(page) {
  const template = document.querySelector(`#page-templates template[data-page-template="${page}"]`);
  return template instanceof HTMLTemplateElement ? template.innerHTML : "";
}

async function getPageMarkup(page) {
  const relativePath = pagePartialPaths[page];
  if (!relativePath) {
    throw new Error(`Unknown page '${page}'`);
  }
  if (pageTemplateCache.has(page)) {
    return pageTemplateCache.get(page);
  }
  const templateMarkup = templateMarkupFromDom(page);
  if (templateMarkup) {
    pageTemplateCache.set(page, templateMarkup);
    return templateMarkup;
  }
  const response = await fetch(`/static/${relativePath}`);
  if (!response.ok) {
    throw new Error(`Failed to load page '${page}': ${response.status}`);
  }
  const markup = await response.text();
  pageTemplateCache.set(page, markup);
  return markup;
}

async function mountPage(page) {
  const container = mainPagesContainer();
  if (!container) return;
  const markup = await getPageMarkup(page);
  container.innerHTML = markup;
  if (document.body) document.body.dataset.page = page;
}

function buildPageUrl(page = state.activePage, overrides = {}) {
  const resolvedPage = pageKinds.includes(page) ? page : currentPageId();
  const dataDirKey = Object.prototype.hasOwnProperty.call(overrides, "dataDirKey")
    ? overrides.dataDirKey
    : state.selectedDataDirKey;
  const runId = Object.prototype.hasOwnProperty.call(overrides, "runId")
    ? overrides.runId
    : state.selectedRunId;
  const entryId = Object.prototype.hasOwnProperty.call(overrides, "entryId")
    ? overrides.entryId
    : state.selectedEntryId;
  const params = new URLSearchParams();
  if (dataDirKey) params.set("dataDir", dataDirKey);
  if (runId) params.set("run", runId);
  params.set("view", resolvedPage);
  if (resolvedPage === "review" && entryId) params.set("entry", entryId);
  const query = params.toString();
  return `${pagePath(resolvedPage)}${query ? `?${query}` : ""}`;
}

async function navigateToPage(page, overrides = {}, options = {}) {
  const resolvedPage = pageKinds.includes(page) ? page : currentPageId();
  const dataDirKey = Object.prototype.hasOwnProperty.call(overrides, "dataDirKey")
    ? overrides.dataDirKey
    : state.selectedDataDirKey;
  const runId = Object.prototype.hasOwnProperty.call(overrides, "runId")
    ? overrides.runId
    : state.selectedRunId;
  const entryId = Object.prototype.hasOwnProperty.call(overrides, "entryId")
    ? overrides.entryId
    : state.selectedEntryId;
  const runChanged = dataDirKey !== state.selectedDataDirKey || runId !== state.selectedRunId;
  const pageChanged = resolvedPage !== currentPageId();

  state.selectedDataDirKey = dataDirKey;
  state.selectedRunId = runId;
  state.selectedEntryId = entryId;
  state.activePage = resolvedPage;

  if (pageChanged) {
    await mountPage(resolvedPage);
  } else if (document.body) {
    document.body.dataset.page = resolvedPage;
  }

  const url = buildPageUrl(resolvedPage, { dataDirKey, runId, entryId });
  if (url !== `${window.location.pathname}${window.location.search}`) {
    if (options.replace) history.replaceState(null, "", url);
    else history.pushState(null, "", url);
  }

  if (resolvedPage === "picker" || runChanged || !state.snapshot) {
    await loadGlobal(dataDirKey, runId, resolvedPage, entryId);
    return;
  }

  renderActivePage();
  renderReviewTabs();
  renderDrawer();
  renderSnapshot(state.snapshot);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function shortText(value, limit = 84) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!text) return "—";
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1)}…`;
}

function toneForStatus(status) {
  if (["failed", "blocked", "merge_blocked", "rolled_back", "rollback"].includes(status)) return "danger";
  if (["awaiting_plan_approval", "awaiting_archive_approval", "stale", "pending_approval", "merge", "archive"].includes(status)) return "warning";
  return "ok";
}

function pill(text, tone = "ok") {
  const klass = tone === "warning" ? "pill warning" : tone === "danger" ? "pill danger" : "pill";
  return `<span class="${klass}">${escapeHtml(text)}</span>`;
}

function formatDate(value) {
  if (!value) return "—";
  return String(value).replace("T", " ").replace("+00:00", " UTC");
}

function valueOrDash(value) {
  return value ? escapeHtml(String(value)) : "—";
}

function actionKey(kind, value = "") {
  return `${kind}:${value}`;
}

function setPendingAction(kind, value = "", pending = true) {
  const key = actionKey(kind, value);
  if (pending) state.pendingActions[key] = true;
  else delete state.pendingActions[key];
}

function storageEntryKey(runId) {
  return `branchclaw:last-entry:${runId}`;
}

function readStoredValue(key) {
  try {
    return window.localStorage.getItem(key) || "";
  } catch (_error) {
    return "";
  }
}

function writeStoredValue(key, value) {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch (_error) {
    // Ignore local storage errors in constrained environments.
  }
}

function rememberSelection() {
  writeStoredValue(storageKeys.dataDir, state.selectedDataDirKey);
  writeStoredValue(storageKeys.run, state.selectedRunId);
  if (state.selectedRunId) writeStoredValue(storageEntryKey(state.selectedRunId), state.selectedEntryId);
}

function rememberedEntry(runId) {
  return runId ? readStoredValue(storageEntryKey(runId)) : "";
}

function parseUrlState() {
  const params = new URLSearchParams(window.location.search);
  return {
    dataDirKey: params.get("dataDir") || "",
    runId: params.get("run") || "",
    view: params.get("view") || "",
    entryId: params.get("entry") || "",
  };
}

function normalizePage() {
  if (!state.selectedRunId) {
    if (!["picker", "control-plane"].includes(state.activePage)) state.activePage = "picker";
    return;
  }
  if (!pageKinds.includes(state.activePage)) state.activePage = "workspace";
}

function syncUrlState() {
  normalizePage();
  history.replaceState(null, "", buildPageUrl(state.activePage));
}

function runAttentionScore(run) {
  if (!run) return 0;
  return (
    (run.pendingApprovals || 0) * 20
    + (run.openInterventionCount || 0) * 20
    + (run.openBatchCount || 0) * 12
    + (run.readyFeatureCount || 0) * 6
  );
}

function pickSmartRun(runs) {
  const rememberedRunId = readStoredValue(storageKeys.run);
  const rememberedDataDirKey = readStoredValue(storageKeys.dataDir);
  const rememberedRun = runs.find(
    (item) => item.id === rememberedRunId && (!rememberedDataDirKey || item.dataDirKey === rememberedDataDirKey),
  );
  if (rememberedRun && runAttentionScore(rememberedRun) > 0) return rememberedRun;
  return [...runs]
    .sort((left, right) => runAttentionScore(right) - runAttentionScore(left))
    .find((item) => runAttentionScore(item) > 0) || null;
}

function isPendingAction(kind, value = "") {
  return Boolean(state.pendingActions[actionKey(kind, value)]);
}

function canRestartWorker(worker) {
  return Boolean(worker && ["stopped", "failed", "blocked"].includes(worker.status));
}

function canArchiveRun(data) {
  if (!data?.run) return false;
  if (data.run.needsReplan) return false;
  if (!["executing", "merge_blocked", "awaiting_plan_approval"].includes(data.run.status)) return false;
  return !(data.workers || []).some((worker) => liveStatuses.has(worker.status) || worker.status === "blocked");
}

function canRestartRun(data) {
  return Boolean(
    data?.run
    && ["executing", "awaiting_plan_approval", "awaiting_archive_approval"].includes(data.run.status),
  );
}

function canCreateWorkspace(data) {
  return Boolean(
    data?.run
    && ["executing", "awaiting_plan_approval", "awaiting_archive_approval"].includes(data.run.status),
  );
}

function selectedDataDir() {
  return state.dataDirs.find((item) => item.dataDirKey === state.selectedDataDirKey) || null;
}

function suggestedWorkerName(data) {
  const used = new Set((data?.workers || []).map((worker) => worker.worker_name));
  const alphabet = "abcdefghijklmnopqrstuvwxyz";
  for (const letter of alphabet) {
    const name = `worker-${letter}`;
    if (!used.has(name)) return name;
  }
  let index = 1;
  while (used.has(`worker-${index}`)) index += 1;
  return `worker-${index}`;
}

function latestEntryIdForWorker(data, workerName) {
  const entries = flattenEntries(data?.worktreeTrack || {})
    .filter((entry) => entry.workerName === workerName)
    .sort((left, right) => (Date.parse(right.recordedAt || "") || 0) - (Date.parse(left.recordedAt || "") || 0));
  return entries[0]?.entryId || "";
}

function workerActionButtons(worker, data) {
  if (!worker) return "—";
  const stopping = isPendingAction("stop-worker", worker.worker_name);
  if (stopping) {
    return `<button type="button" disabled>Stopping…</button>`;
  }
  if (liveStatuses.has(worker.status)) {
    return `<button data-action="stop-worker" data-worker="${escapeHtml(worker.worker_name)}" type="button">Stop</button>`;
  }
  const actions = [];
  if (canArchiveRun(data)) {
    actions.push(`<button data-action="archive" type="button">Archive</button>`);
  }
  if (canRestartWorker(worker) && canRestartRun(data)) {
    actions.push(`<button class="ghost" data-action="restart-worker" data-worker="${escapeHtml(worker.worker_name)}" type="button">Restart</button>`);
  }
  return actions.length ? actions.join("") : "—";
}

function renderWorkspaceEntryButtons(data) {
  const visible = Boolean(data?.run);
  const enabled = canCreateWorkspace(data);
  for (const id of ["new-workspace-button"]) {
    const button = byId(id);
    if (!button) continue;
    button.classList.toggle("hidden", !visible);
    button.disabled = !enabled;
    button.title = enabled ? "" : "This run status does not allow new worktrees.";
  }
}

function renderDrawer() {
  const backdrop = byId("drawer-backdrop");
  const runForm = byId("new-run-form");
  const workspaceForm = byId("new-workspace-form");
  const title = byId("drawer-title");
  const description = byId("drawer-description");
  if (!backdrop || !runForm || !workspaceForm || !title || !description) return;
  const kind = state.openDrawer;
  backdrop.classList.toggle("hidden", !kind);
  runForm.classList.toggle("hidden", kind !== "new-run");
  workspaceForm.classList.toggle("hidden", kind !== "new-workspace");
  if (kind === "new-run") {
    title.textContent = "New Run";
    description.textContent = "Create a run in a selected data dir and submit its first plan proposal.";
  } else if (kind === "new-workspace") {
    title.textContent = "Add Worktree";
    description.textContent = "Create a new worker worktree in the selected run's current stage.";
  }
}

function syncNewRunDataDirMode() {
  const select = byId("new-run-data-dir-select");
  const manualBlock = byId("new-run-data-dir-manual-block");
  const error = byId("new-run-data-dir-error");
  if (!select || !manualBlock || !error) return;
  manualBlock.classList.toggle("hidden", select.value !== "__manual__");
  error.textContent = "";
  error.classList.add("hidden");
}

function populateNewRunForm() {
  const select = byId("new-run-data-dir-select");
  const manualInput = byId("new-run-data-dir-manual");
  if (!select || !manualInput) return;
  const trackedOptions = state.dataDirs.map((item) => `
    <option value="${escapeHtml(item.dataDirKey)}">${escapeHtml(shortText(item.dataDir, 68))}</option>
  `).join("");
  select.innerHTML = `${trackedOptions}<option value="__manual__">Manual .branchclaw path…</option>`;
  if (state.selectedDataDirKey && state.dataDirs.some((item) => item.dataDirKey === state.selectedDataDirKey)) {
    select.value = state.selectedDataDirKey;
    manualInput.value = selectedDataDir()?.dataDir || "";
  } else {
    select.value = "__manual__";
    manualInput.value = "";
  }
  const newRunRepo = byId("new-run-repo");
  const newRunName = byId("new-run-name");
  const newRunDirection = byId("new-run-direction");
  const newRunIntegrationRef = byId("new-run-integration-ref");
  const newRunMaxFeatures = byId("new-run-max-features");
  const newRunDescription = byId("new-run-description");
  const newRunProfile = byId("new-run-profile");
  const newRunSpec = byId("new-run-spec");
  const newRunRules = byId("new-run-rules");
  const newRunPlan = byId("new-run-plan");
  if (newRunRepo) newRunRepo.value = state.snapshot?.run?.repoRoot || "";
  if (newRunName) newRunName.value = "";
  if (newRunDirection) newRunDirection.value = state.snapshot?.run?.direction || "";
  if (newRunIntegrationRef) newRunIntegrationRef.value = state.snapshot?.run?.integrationRef || "";
  if (newRunMaxFeatures) newRunMaxFeatures.value = String(state.snapshot?.run?.maxActiveFeatures || 2);
  if (newRunDescription) newRunDescription.value = "";
  if (newRunProfile) newRunProfile.value = state.snapshot?.run?.projectProfile || "web";
  if (newRunSpec) newRunSpec.value = "";
  if (newRunRules) newRunRules.value = "";
  if (newRunPlan) newRunPlan.value = "";
  syncNewRunDataDirMode();
}

function populateNewWorkspaceForm() {
  const run = state.snapshot?.run || {};
  const context = byId("new-workspace-context");
  const featureId = byId("new-workspace-feature-id");
  const name = byId("new-workspace-name");
  const backend = byId("new-workspace-backend");
  const command = byId("new-workspace-command");
  const task = byId("new-workspace-task");
  const skipPermissions = byId("new-workspace-skip-permissions");
  if (context) context.value = `${run.id || "—"} · ${run.currentStageId || "stage"} · current stage`;
  if (featureId) featureId.value = "";
  if (name) name.value = suggestedWorkerName(state.snapshot);
  if (backend) backend.value = "tmux";
  if (command) command.value = "claude";
  if (task) task.value = "";
  if (skipPermissions) skipPermissions.checked = true;
}

function openDrawer(kind) {
  if (kind === "new-workspace" && !canCreateWorkspace(state.snapshot)) {
    window.alert("The selected run cannot accept a new worktree in its current status.");
    return;
  }
  state.openDrawer = kind;
  if (kind === "new-run") populateNewRunForm();
  if (kind === "new-workspace") populateNewWorkspaceForm();
  renderDrawer();
}

function closeDrawer() {
  state.openDrawer = "";
  renderDrawer();
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return response.json();
}

async function postJson(url, payload = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  let body = {};
  if (text) {
    try {
      body = JSON.parse(text);
    } catch (_error) {
      body = { error: text };
    }
  }
  if (!response.ok) throw new Error(body.error || `Request failed: ${response.status}`);
  return body;
}

function scopedRunUrl() {
  return `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}`;
}

function scopedEventsUrl() {
  return `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/events/${encodeURIComponent(state.selectedRunId)}`;
}

function scopedRecentEventsUrl() {
  return `${scopedRunUrl()}/recent-events?limit=30`;
}

function readActor() {
  const input = byId("operator-actor");
  return (input && input.value.trim()) || "dashboard";
}

function defaultFeedback() {
  const input = byId("approval-feedback-default");
  return (input && input.value.trim()) || "";
}

function gateFeedback(gateId) {
  const input = byId(`approval-feedback-${gateId}`);
  return (input && input.value.trim()) || defaultFeedback();
}

function archiveFormPayload() {
  const labelInput = byId("review-archive-label");
  const summaryInput = byId("review-archive-summary");
  return {
    label: (labelInput && labelInput.value.trim()) || "dashboard-checkpoint",
    summary: (summaryInput && summaryInput.value.trim()) || "",
  };
}

function closeEventSource() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

function clearRunSnapshot() {
  state.snapshot = null;
  state.snapshotSignature = "";
  state.recentEvents = [];
  state.recentEventsSignature = "";
  state.selectedEntryId = "";
}

function flattenEntries(track) {
  const rows = [];
  for (const group of track?.tracks || []) {
    for (const entry of group.entries || []) {
      rows.push({ ...entry, workerName: entry.workerName || group.workerName });
    }
  }
  return rows;
}

function flattenEntryMap(track) {
  const mapping = {};
  for (const entry of flattenEntries(track)) mapping[entry.entryId] = entry;
  return mapping;
}

function sortedEntries(track) {
  return [...flattenEntries(track)].sort((left, right) => {
    const leftAt = Date.parse(left.recordedAt || "") || 0;
    const rightAt = Date.parse(right.recordedAt || "") || 0;
    return rightAt - leftAt;
  });
}

function entryById(data, entryId) {
  return flattenEntryMap(data?.worktreeTrack || {})[entryId] || null;
}

function pickDefaultEntry(data) {
  const entries = sortedEntries(data?.worktreeTrack || {});
  if (!entries.length) return "";
  for (const intervention of data.interventions || []) {
    if (intervention.status === "open" && intervention.relatedEntryId) {
      const related = entries.find((entry) => entry.entryId === intervention.relatedEntryId);
      if (related) return related.entryId;
    }
  }
  for (const approval of data.approvals || []) {
    const related = (approval.relatedEntryIds || []).find((entryId) => entries.some((entry) => entry.entryId === entryId));
    if (related) return related;
  }
  const currentAccepted = entries.find((entry) => ["current", "restored"].includes(entry.kind) && entry.resultStatus);
  if (currentAccepted) return currentAccepted.entryId;
  const anyAccepted = entries.find((entry) => entry.resultStatus);
  if (anyAccepted) return anyAccepted.entryId;
  return entries[0].entryId;
}

function ensureSelectedEntry(data) {
  const entries = flattenEntries(data?.worktreeTrack || {});
  if (!entries.length) {
    state.selectedEntryId = "";
    return null;
  }
  const remembered = rememberedEntry(data?.run?.id || "");
  const preferredEntryId = state.selectedEntryId || remembered;
  if (!preferredEntryId || !entries.some((entry) => entry.entryId === preferredEntryId)) {
    state.selectedEntryId = pickDefaultEntry(data);
  } else {
    state.selectedEntryId = preferredEntryId;
  }
  rememberSelection();
  return entryById(data, state.selectedEntryId);
}

function matchingWorker(data, entry) {
  if (!entry) return null;
  return (data.workers || []).find((worker) => worker.worker_name === entry.workerName) || null;
}

function matchingArchive(data, entry) {
  if (!entry || !entry.archiveId) return null;
  return (data.archives || []).find((archive) => archive.id === entry.archiveId) || null;
}

function approvalPriority(approval, selectedEntry) {
  if (!selectedEntry) return 1;
  return (approval.relatedEntryIds || []).includes(selectedEntry.entryId) ? 0 : 1;
}

function approvalContextSummary(approval) {
  const workerPart = (approval.relatedWorkerNames || []).length
    ? `Worker ${approval.relatedWorkerNames.join(", ")}`
    : "Run-wide context";
  const archivePart = approval.relatedArchiveId
    ? `archive ${approval.relatedArchiveId}`
    : (approval.stage_id ? `stage ${approval.stage_id}` : "");
  return [workerPart, archivePart].filter(Boolean).join(" · ");
}

function nodeTone(entry) {
  if (["failed", "blocked"].includes(entry.resultStatus) || ["failed", "blocked"].includes(entry.status)) return "danger";
  if (entry.kind === "archived" || entry.kind === "restored" || entry.status === "pending_approval") return "warning";
  return "ok";
}

function eventMatchesEntry(event, entry) {
  if (!entry) return true;
  const payload = event.payload || {};
  const workerName =
    payload.worker_name ||
    payload.workerName ||
    payload.worker?.worker_name ||
    payload.worker?.workerName ||
    "";
  const archiveId =
    payload.archive_id ||
    payload.archiveId ||
    payload.target_id ||
    payload.targetId ||
    payload.archive?.id ||
    "";
  const stageId = payload.stage_id || payload.stageId || payload.stage?.id || "";
  if (entry.archiveId && archiveId && archiveId === entry.archiveId) return true;
  if (workerName && entry.workerName && workerName === entry.workerName) return true;
  if (!entry.archiveId && stageId && entry.stageId && stageId === entry.stageId) return true;
  return false;
}

function eventSubject(payload) {
  const workerName =
    payload.worker_name ||
    payload.workerName ||
    payload.worker?.worker_name ||
    payload.worker?.workerName;
  if (workerName) return workerName;
  const archiveId = payload.archive_id || payload.archiveId || payload.archive?.id;
  if (archiveId) return shortText(archiveId, 24);
  const gateId = payload.gate_id || payload.gateId || payload.approval?.id || payload.id;
  if (gateId) return shortText(gateId, 24);
  const planId = payload.plan_id || payload.planId || payload.plan?.id;
  if (planId) return shortText(planId, 24);
  return "System";
}

function describeEvent(event) {
  const payload = event.payload || {};
  const type = event.event_type || "";
  const subject = eventSubject(payload);
  const archiveId = payload.archive_id || payload.archiveId || payload.archive?.id || payload.target_id || payload.targetId || "";
  const stageId = payload.stage_id || payload.stageId || payload.stage?.id || "";
  const toolName = payload.tool_name || payload.toolName || "";
  const status = payload.status || payload.result?.status || payload.runtime_status || "";
  const previewUrl = payload.preview_url || payload.previewUrl || payload.result?.preview_url || payload.result?.previewUrl || "";
  const reason = payload.reason || payload.failure_reason || payload.failureReason || payload.error || "";
  const related = [stageId ? `stage ${stageId}` : "", archiveId ? `archive ${shortText(archiveId, 24)}` : ""]
    .filter(Boolean)
    .join(" · ");
  const fallbackDetail = shortText(JSON.stringify(payload), 220);

  const byType = {
    "worker.started": [`${subject} started`, related || "Worker runtime is now active."],
    "worker.restarted": [`${subject} restarted`, related || "Worker runtime restarted from the last launch payload."],
    "worker.stopped": [`${subject} stopped`, reason || related || "Worker runtime stopped cleanly."],
    "worker.failed": [`${subject} failed`, reason || related || "Worker runtime ended in failure."],
    "worker.stale": [`${subject} missed heartbeats`, related || "Worker was marked stale and needs reconciliation."],
    "worker.reconciled": [`${subject} reconciled to ${status || "current"} state`, related || "Runtime state was refreshed from process observations."],
    "worker.exited": [`${subject} exited`, reason || related || "Worker child process exited."],
    "worker.reported": [`${subject} reported ${status || "a result"}`, previewUrl ? `Preview ${previewUrl}` : related || "A structured result was recorded."],
    "worker.tool_called": [`${subject} ran ${toolName || "a tool"}`, related || "Tool execution started."],
    "worker.tool_completed": [`${subject} completed ${toolName || "a tool"}`, previewUrl ? `Preview ${previewUrl}` : status || related || "Tool execution completed successfully."],
    "worker.tool_failed": [`${subject} failed ${toolName || "a tool"}`, reason || fallbackDetail],
    "worker.remediation_attempted": [`${subject} tried an automatic recovery`, `${toolName || "tool"} · ${reason || fallbackDetail}`],
    "worker.remediation_succeeded": [`${subject} recovered automatically`, `${toolName || "tool"} · ${related || "Remediation succeeded."}`],
    "worker.remediation_failed": [`${subject} could not recover automatically`, `${toolName || "tool"} · ${reason || fallbackDetail}`],
    "worker.intervention_opened": [`${subject} needs manual intervention`, `${payload.recommended_action || "Review required"} · ${reason || fallbackDetail}`],
    "worker.intervention_resolved": [`${subject} intervention resolved`, related || "Manual queue item closed."],
    "archive.requested": [`Archive requested for ${subject}`, related || "Awaiting approval before restore or merge."],
    "archive.approved": [`Archive approved for ${subject}`, related || "Archive is ready for restore or merge."],
    "archive.restored": [`Archive restored for ${subject}`, related || "A restored worktree was created from this archive."],
    "approval.requested": [`Decision requested for ${subject}`, `${payload.gate_type || "gate"} · ${related || fallbackDetail}`],
    "approval.approved": [`Decision approved for ${subject}`, `${payload.gate_type || "gate"} · ${related || fallbackDetail}`],
    "approval.rejected": [`Decision rejected for ${subject}`, `${payload.gate_type || "gate"} · ${reason || fallbackDetail}`],
    "plan.proposed": ["Plan proposed", `${subject} · ${related || fallbackDetail}`],
    "plan.replan_requested": ["Replan requested", `${reason || fallbackDetail}`],
    "constraint.added": ["Constraint added", shortText(payload.content || fallbackDetail, 180)],
    "merge.requested": ["Merge requested", related || fallbackDetail],
    "merge.approved": ["Merge approved", related || fallbackDetail],
  };

  const pair = byType[type];
  if (pair) return { summary: pair[0], detail: pair[1] };
  return {
    summary: `${subject} · ${type.split(".").join(" ")}`,
    detail: related || fallbackDetail,
  };
}

function eventLevel(event) {
  const explicit = String(event.level || event.log_level || "").toLowerCase().trim();
  if (["info", "warning", "error"].includes(explicit)) return explicit;
  if (explicit === "debug") return "info";
  const type = String(event.event_type || "").toLowerCase();
  const payload = event.payload || {};
  const status = String(payload.status || payload.result?.status || "").toLowerCase();
  if (type.includes("failed") || type.includes("rejected") || type.includes("blocked") || type.includes("intervention_opened")) return "error";
  if (status.includes("failed") || status.includes("blocked") || status.includes("error")) return "error";
  if (
    type.includes("requested")
    || type.includes("stale")
    || type.includes("replan")
    || type.includes("remediation_attempted")
    || type.includes("remediation_failed")
    || type.includes("superseded")
    || type.includes("merge.blocked")
    || status === "pending"
    || status === "pending_approval"
    || status === "stale"
  ) return "warning";
  return "info";
}

function renderIndexBar() {
  const indexShell = document.querySelector(".index-shell");
  const summaryStrip = byId("summary-strip");
  const pageIndex = byId("page-index");
  if (!summaryStrip || !pageIndex || !indexShell) return;
  const hiddenOnPicker = state.activePage === "picker";
  indexShell.classList.toggle("hidden", hiddenOnPicker);
  if (hiddenOnPicker) {
    summaryStrip.innerHTML = "";
    pageIndex.innerHTML = "";
    return;
  }
  const daemon = state.daemon || {};
  const runs = state.runs || [];
  const selectedRun = state.snapshot?.run || null;
  const selectedTrackSummary = state.snapshot?.worktreeTrack?.summary || {};
  const decisions = selectedRun ? (state.snapshot?.approvals?.length || 0) : runs.reduce((sum, item) => sum + (item.pendingApprovals || 0), 0);
  const chips = selectedRun ? [
    {
      label: "Current Run",
      value: selectedRun.name || selectedRun.id || "—",
      meta: `${selectedRun.status} · ${selectedTrackSummary.trackedWorkers || 0} worktree track(s)`,
    },
    {
      label: "Needs Review",
      value: `${decisions}`,
      meta: `${selectedRun.openInterventionCount || 0} intervention(s)`,
    },
  ] : [
    {
      label: "Tracked",
      value: `${state.dataDirs.length} workdir(s)`,
      meta: `${runs.length} run(s) available`,
    },
    {
      label: "Control Plane",
      value: daemon.running ? "Ready" : "Stopped",
      meta: `${decisions} open decision(s) across tracked runs`,
    },
  ];
  const pages = [
    ...(selectedRun ? [
      {
        key: "workspace",
        title: "Workspace",
        meta: `${selectedRun.name || selectedRun.id} · graph, queue, and batches`,
      },
      {
        key: "review",
        title: "Review",
        meta: state.selectedEntryId
          ? "Selected worktree · decisions and evidence"
          : "Open a worktree from the graph to review it",
      },
    ] : [
      {
        key: "picker",
        title: "Picker",
        meta: runs.length ? "Choose a workdir and run." : "Create your first run.",
      },
    ]),
    {
      key: "control-plane",
      title: "Control Plane",
      meta: `${runs.reduce((sum, item) => sum + (item.openInterventionCount || 0), 0)} open interventions across tracked runs`,
    },
  ];
  summaryStrip.innerHTML = chips.map((card) => `
    <article class="summary-chip">
      <span class="label">${escapeHtml(card.label)}</span>
      <p class="value">${escapeHtml(card.value)}</p>
      <div class="meta">${escapeHtml(shortText(card.meta, 52))}</div>
    </article>
  `).join("");
  pageIndex.innerHTML = pages.map((page) => `
    <button class="page-tab ${state.activePage === page.key ? "active" : ""}" data-action="show-page" data-page="${page.key}" type="button">
      <strong>${escapeHtml(page.title)}</strong>
      <span class="meta">${escapeHtml(shortText(page.meta, 88))}</span>
    </button>
  `).join("");
}

function renderActivePage() {
  normalizePage();
  if (document.body) document.body.dataset.page = state.activePage;
  syncUrlState();
}

function renderGlobalCards() {
  renderIndexBar();
}

function renderReviewTabs() {
  for (const tab of reviewTabs) {
    const panel = byId(`review-tab-${tab}`);
    if (panel) panel.classList.toggle("hidden", state.reviewTab !== tab);
  }
  document.querySelectorAll('[data-action="show-review-tab"]').forEach((button) => {
    if (!(button instanceof HTMLElement)) return;
    button.classList.toggle("active", button.dataset.tab === state.reviewTab);
  });
}

function renderPickerPanel() {
  const target = byId("picker-panel");
  if (!target) return;
  if (!state.dataDirs.length && !state.runs.length) {
    target.innerHTML = `
      <section class="panel picker-card">
        <h3>No workdirs are tracked yet</h3>
        <p class="hint">A workdir is a <span class="mono">.branchclaw</span> home. Create a run to register one and start working from worktrees.</p>
        <div class="actions">
          <button data-action="open-new-run" type="button">Create Run</button>
        </div>
      </section>
    `;
    return;
  }

  const dataDirByKey = Object.fromEntries((state.dataDirs || []).map((item) => [item.dataDirKey, item]));
  const grouped = new Map();
  for (const run of state.runs || []) {
    const key = run.dataDirKey || "__unknown__";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(run);
  }
  for (const item of state.dataDirs || []) {
    if (!grouped.has(item.dataDirKey)) grouped.set(item.dataDirKey, []);
  }

  const cards = [...grouped.entries()].map(([key, runs]) => {
    const info = dataDirByKey[key] || { dataDirKey: key, dataDir: key, pendingApprovals: 0, liveWorkers: 0 };
    const choices = runs.length
      ? `
        <div class="run-choice-list">
          ${runs
            .sort((left, right) => left.name.localeCompare(right.name))
            .map((run) => `
              <button
                class="run-choice ${state.selectedRunId === run.id && state.selectedDataDirKey === run.dataDirKey ? "selected" : ""}"
                data-action="select-run"
                data-data-dir="${escapeHtml(run.dataDirKey)}"
                data-run="${escapeHtml(run.id)}"
                type="button"
              >
                <strong>${escapeHtml(run.name || run.id)}</strong>
                <span class="meta">${escapeHtml(run.status)} · ${(run.pendingApprovals || 0)} decision(s) · ${(run.openInterventionCount || 0)} intervention(s)</span>
              </button>
            `).join("")}
        </div>
      `
      : `<div class="review-empty">No runs yet in this workdir. Create one to start from worktrees.</div>`;
    return `
      <section class="panel picker-card">
        <div class="picker-workdir">
          <span class="picker-kicker">Workdir</span>
          <div>
            <h3>${escapeHtml(shortText(info.dataDir, 44))}</h3>
            <div class="mono">${escapeHtml(info.dataDir)}</div>
          </div>
          <div class="inline-stats">
            <span>${runs.length} run(s)</span>
            <span>${info.pendingApprovals || 0} pending decision(s)</span>
            <span>${info.liveWorkers || 0} live worker(s)</span>
          </div>
          <div class="actions">
            <button
              class="ghost"
              data-action="open-new-run"
              data-data-dir="${escapeHtml(info.dataDirKey)}"
              type="button"
            >Create Run Here</button>
          </div>
        </div>
        <div class="picker-runs">
          <div class="picker-runs-head">
            <strong>${runs.length ? "Choose a run" : "No runs yet"}</strong>
            <span class="compact">${runs.length ? "Open a run to work from its worktrees." : "This workdir is ready for its first run."}</span>
          </div>
          ${choices}
        </div>
      </section>
    `;
  });

  target.innerHTML = cards.join("");
}

function renderFeatureQueue(data) {
  const target = byId("feature-queue-panel");
  if (!target) return;
  const features = [...(data.features || [])].sort((a, b) => (a.priority || 0) - (b.priority || 0));
  if (!features.length) {
    target.innerHTML = `<div class="review-empty">No planner-managed features yet. Approve a plan with explicit feature blocks to start backlog dispatch.</div>`;
    return;
  }
  target.innerHTML = `<div class="archive-grid">${features.map((feature) => `
    <div class="archive-card">
      <div class="header-line">
        <strong>${escapeHtml(feature.title || feature.id)}</strong>
        ${pill(feature.status, toneForStatus(feature.status))}
      </div>
      <div class="compact">priority ${escapeHtml(String(feature.priority || 0))} · worker ${escapeHtml(feature.worker_name || "—")}</div>
      <div class="muted">${escapeHtml(shortText(feature.goal || feature.task || "", 180))}</div>
      <div class="archive-meta">
        <div><span class="label">Areas</span><div>${escapeHtml((feature.claimed_areas || []).join(", ") || "—")}</div></div>
        <div><span class="label">Validation</span><div>${escapeHtml(feature.validation_status || "pending")}</div></div>
      </div>
    </div>
  `).join("")}</div>`;
}

function renderBatchReview(data) {
  const target = byId("batch-review-panel");
  if (!target) return;
  const batches = [...(data.batches || [])];
  if (!batches.length) {
    target.innerHTML = `<div class="review-empty">No review batches yet. Ready features will be grouped here for periodic review.</div>`;
    return;
  }
  target.innerHTML = `<div class="archive-grid">${batches.map((batch) => `
    <div class="archive-card">
      <div class="header-line">
        <strong class="mono" title="${escapeHtml(batch.id)}">${escapeHtml(shortText(batch.id, 26))}</strong>
        ${pill(batch.status, toneForStatus(batch.status))}
      </div>
      <div class="compact">${escapeHtml(String((batch.feature_ids || []).length))} feature(s) · integration ${escapeHtml(batch.integration_ref || "—")}</div>
      <div class="muted">${escapeHtml(shortText((batch.featureSummaries || []).map((item) => item.title).join(", "), 180))}</div>
      <div class="archive-actions">
        ${batch.status === "pending_approval" ? `<button data-action="merge-batch" data-batch="${escapeHtml(batch.id)}" type="button">Request Merge</button>` : ""}
        ${batch.status === "pending_promote" ? `<button data-action="promote-batch" data-batch="${escapeHtml(batch.id)}" type="button">Request Promote</button>` : ""}
      </div>
    </div>
  `).join("")}</div>`;
}

function renderRunHomeHeader(data) {
  const run = data.run || {};
  const workdir = selectedDataDir();
  const trackSummary = data.worktreeTrack?.summary || {};
  const decisionCount = data.approvals?.length || 0;
  const interventionCount = data.run?.openInterventionCount || 0;
  const markup = `
    <div class="run-home-top">
      <div>
        <div class="run-home-breadcrumb">
          <button class="ghost" data-action="go-home" type="button">Back to Workdirs</button>
          <span>${escapeHtml(shortText(workdir?.dataDir || run.ownerDataDir || "Workdir", 56))}</span>
          <span>›</span>
          <strong>${escapeHtml(run.name || run.id || "Run")}</strong>
        </div>
        <h2>${escapeHtml(run.name || run.id || "Run")}</h2>
        <p class="hint">${escapeHtml(run.description || "Review this run through its worktrees. Select a worktree node to inspect result, preview, and next action.")}</p>
      </div>
      <div class="run-home-actions">
        <button ${canCreateWorkspace(data) ? "" : "disabled"} data-action="open-new-workspace" type="button">Add Worktree</button>
      </div>
    </div>
    <div class="inline-stats">
      <span>${trackSummary.trackedWorkers || 0} worktree track(s)</span>
      <span>${decisionCount} decision(s) waiting on this run</span>
      <span>${interventionCount} intervention(s)</span>
      <span>${escapeHtml(run.status || "unknown")}</span>
    </div>
  `;
  for (const id of ["run-home-header", "review-page-header"]) {
    const target = byId(id);
    if (target) target.innerHTML = markup;
  }
}

function renderSelectedDetailPanel(data, selectedEntry) {
  const target = byId("selected-detail-panel");
  if (!target) return;
  if (!selectedEntry) {
    target.innerHTML = `<div class="review-empty">Select a worktree to inspect its branch, archive lineage, and architecture summary.</div>`;
    return;
  }
  const archive = matchingArchive(data, selectedEntry);
  const architectureSummary = selectedEntry.architectureSummary || "";
  target.innerHTML = `
    <div class="review-shell">
      <div class="review-block">
        <h3>Context</h3>
        <p>
          Worktree: <span class="mono">${escapeHtml(selectedEntry.workerName)}</span><br>
          Kind: ${escapeHtml(selectedEntry.kind)}<br>
          Path: <span class="mono">${escapeHtml(selectedEntry.relativePath || selectedEntry.workspacePath || "—")}</span><br>
          Branch: <span class="mono">${escapeHtml(shortText(selectedEntry.branch || "—", 88))}</span><br>
          ${archive ? `Archive: <span class="mono">${escapeHtml(archive.id)}</span> · ${escapeHtml(archive.label || "unlabeled")}` : `Stage: <span class="mono">${escapeHtml(selectedEntry.stageId || "—")}</span>`}
        </p>
      </div>
      <div class="review-block">
        <h3>Architecture</h3>
        ${architectureSummary
          ? `<pre>${escapeHtml(architectureSummary)}</pre>`
          : `<div class="review-empty">No architecture summary recorded for this worktree.</div>`}
      </div>
    </div>
  `;
}

function renderRunInterventionStrip(data) {
  const panel = byId("run-interventions-panel");
  const target = byId("run-interventions-list");
  if (!panel || !target) return;
  const interventions = (data.interventions || []).filter((item) => item.status === "open");
  panel.classList.toggle("hidden", interventions.length === 0);
  if (!interventions.length) {
    target.innerHTML = "";
    return;
  }
  target.innerHTML = interventions.map((item) => `
    <article class="attention-card">
      <strong>${escapeHtml(item.worker_name)}</strong>
      <p class="hint">${escapeHtml(shortText(item.reason || "Needs operator attention.", 120))}</p>
      <div class="tree-node-badges">
        ${pill(item.recommended_action || "manual", "warning")}
        ${item.last_tool_name ? pill(item.last_tool_name, "ok") : ""}
      </div>
      <div class="actions">
        ${item.relatedEntryId ? `<button class="ghost" data-action="open-review" data-entry="${escapeHtml(item.relatedEntryId)}" type="button">Open Review</button>` : ""}
        <button class="ghost" data-action="reconcile" type="button">Reconcile</button>
      </div>
    </article>
  `).join("");
}

function renderDaemonOverview() {
  const target = byId("daemon-overview");
  if (!target) return;
  const daemon = state.daemon || {};
  const openInterventions = (state.runs || []).reduce((sum, item) => sum + (item.openInterventionCount || 0), 0);
  const entries = [
    ["Daemon", daemon.running ? pill("running") : pill("stopped", "warning")],
    ["Dashboard", `<span class="mono">${escapeHtml(daemon.dashboard_url || "—")}</span>`],
    ["Workdirs", String(state.dataDirs.length)],
    ["Runs", String(state.runs.length)],
    ["Processes", String(state.processes.length)],
    ["Interventions", String(openInterventions)],
  ];
  target.innerHTML = entries.map(([label, value]) => `
    <div class="item"><span class="label">${label}</span><div class="value">${value}</div></div>
  `).join("");
}

function renderDataDirs() {
  const target = byId("data-dirs-panel");
  if (!target) return;
  const rows = state.dataDirs.map((item) => `
    <tr>
      <td><span class="mono">${escapeHtml(item.dataDirKey)}</span></td>
      <td><span class="mono" title="${escapeHtml(item.dataDir)}">${escapeHtml(shortText(item.dataDir, 64))}</span></td>
      <td>${item.runCount}</td>
      <td>${item.processCount}</td>
      <td>${item.liveWorkers}</td>
      <td>${item.pendingApprovals}</td>
    </tr>
  `);
  target.innerHTML = state.dataDirs.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th>Key</th><th>Workdir</th><th>Runs</th><th>Processes</th><th>Live Workers</th><th>Pending Decisions</th></tr></thead>
      <tbody>${rows.join("")}</tbody>
    </table></div>` : `<p class="hint">No managed data dirs yet.</p>`;
}

function renderProcesses() {
  const target = byId("processes-panel");
  if (!target) return;
  const rows = state.processes.map((item) => {
    const endpoint = item.socket || (item.port ? `${item.host}:${item.port}` : "—");
    const owner = item.worker_name ? `${item.run_id}/${item.worker_name}` : (item.run_id || "—");
    return `
      <tr>
        <td><span class="mono">${escapeHtml(item.id)}</span></td>
        <td>${escapeHtml(item.process_kind)}</td>
        <td><span class="mono">${escapeHtml(shortText(item.data_dir, 48))}</span></td>
        <td>${escapeHtml(owner)}</td>
        <td><span class="mono">${escapeHtml(String(item.supervisor_pid || item.pid || "-"))}</span></td>
        <td><span class="mono">${escapeHtml(endpoint)}</span></td>
        <td>${pill(item.status || "-", toneForStatus(item.status || ""))}</td>
      </tr>`;
  });
  target.innerHTML = state.processes.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th>ID</th><th>Kind</th><th>Data Dir</th><th>Run / Worker</th><th>PID</th><th>Endpoint</th><th>Status</th></tr></thead>
      <tbody>${rows.join("")}</tbody>
    </table></div>` : `<p class="hint">No managed processes.</p>`;
}

function renderRunOverview(data) {
  const element = byId("run-overview");
  if (!element) return;
  const entries = [
    ["Run", `<span class="mono">${escapeHtml(data.run.id)}</span>`],
    ["Status", pill(data.run.status, toneForStatus(data.run.status))],
    ["Profile", pill(data.run.projectProfile || "-", "ok")],
    ["Current Stage", escapeHtml(data.run.currentStageId || "-")],
    ["Active Plan", `<span class="mono">${escapeHtml(data.run.activePlanId || "(none)")}</span>`],
    ["Replan", data.run.needsReplan ? `${pill(data.run.dirtyReason || "constraint", "warning")}<br><span class="mono">${escapeHtml(data.run.dirtySince || "")}</span>` : pill("clean")],
    ["Repo", `<span class="mono" title="${escapeHtml(data.run.repoRoot || "-")}">${escapeHtml(shortText(data.run.repoRoot || "-", 56))}</span><br><span class="mono">${escapeHtml(data.run.baseRef || "-")}</span>`],
    ["Managed", `${data.run.managedProcessCount || 0} process(es)<br>${escapeHtml((data.run.managedProcessKinds || []).join(", ") || "—")}`],
    ["Interventions", `${data.run.openInterventionCount || 0} open`],
    ["Last Event", `<span class="mono">${escapeHtml(formatDate(data.lastEventAt || ""))}</span>`],
  ];
  element.innerHTML = entries.map(([label, value]) => `
    <div class="item"><span class="label">${label}</span><div class="value">${value}</div></div>
  `).join("");
}

function renderWorkers(data, selectedEntry) {
  const target = byId("workers-panel");
  if (!target) return;
  const cards = data.workers.map((worker) => {
    const contextual = selectedEntry && worker.worker_name === selectedEntry.workerName;
    return `
      <article class="worker-card ${contextual ? "selected" : ""}">
        <div class="worker-card-head">
          <div>
            <strong>${escapeHtml(worker.worker_name)}</strong>
            <div class="compact">${escapeHtml(worker.backend)} · heartbeat ${escapeHtml(`${worker.heartbeatAgeSeconds ?? 0}s`)}</div>
          </div>
          <div class="tree-node-badges">
            ${pill(worker.status, toneForStatus(worker.status))}
            ${worker.resultStatus ? pill(worker.resultStatus, toneForStatus(worker.resultStatus)) : ""}
          </div>
        </div>
        <div class="worker-card-grid">
          <div><span class="label">Preview</span><div>${worker.previewUrl ? `<a href="${escapeHtml(worker.previewUrl)}" target="_blank" rel="noreferrer">${escapeHtml(worker.previewUrl)}</a>` : escapeHtml(worker.discoveredUrl || "—")}</div></div>
          <div><span class="label">Tool</span><div>${escapeHtml(shortText(worker.lastToolName ? `${worker.lastToolName}:${worker.lastToolStatus || "idle"}` : "—", 48))}</div></div>
          <div><span class="label">PIDs</span><div class="mono">${escapeHtml(`${worker.supervisor_pid || 0}/${worker.child_pid || worker.pid || 0}`)}</div></div>
          <div><span class="label">Report</span><div>${escapeHtml(worker.reportSource || "—")}</div></div>
        </div>
        <div class="worker-card-task">${escapeHtml(shortText(worker.task, 180))}</div>
        <div class="actions">${workerActionButtons(worker, data)}</div>
      </article>
    `;
  });
  target.innerHTML = data.workers.length
    ? `<div class="worker-stack">${cards.join("")}</div>`
    : `<div class="review-empty">No worktrees have been added to this run yet.</div>`;
}

function renderConstraints(data) {
  const target = byId("constraints-panel");
  if (!target) return;
  const rows = data.constraints.map((item) => `<li><strong class="mono">${escapeHtml(item.id)}</strong><br>${escapeHtml(item.content)}<br><span class="mono">${escapeHtml(formatDate(item.created_at))}</span></li>`);
  target.innerHTML = `<div class="tree-groups">${rows.length ? rows.join("") : '<div class="review-empty">No constraints.</div>'}</div>`;
}

function renderInterventionsPage(data) {
  const contextLine = byId("intervention-context-line");
  const interventionsPanel = byId("interventions-panel");
  const runsPanel = byId("intervention-runs-panel");
  if (!contextLine || !interventionsPanel || !runsPanel) return;
  const interventions = (data?.interventions || []).filter((item) => item.status === "open");
  const runsNeedingAttention = (state.runs || []).filter((item) => (item.openInterventionCount || 0) > 0);
  contextLine.textContent = interventions.length
    ? `${interventions.length} open intervention(s) in the selected run.`
    : "No open interventions in the selected run.";
  interventionsPanel.innerHTML = interventions.length ? interventions.map((item) => {
    const worker = (data?.workers || []).find((entry) => entry.worker_name === item.worker_name) || null;
    const relatedEntry = item.relatedEntryId && data ? entryById(data, item.relatedEntryId) : null;
    const canArchive = data ? canArchiveRun(data) : false;
    const canRestart = data ? (canRestartRun(data) && canRestartWorker(worker)) : false;
    const restarting = worker ? isPendingAction("restart-worker", worker.worker_name) : false;
    return `
      <article class="intervention-card">
        <div class="title-row">
          <div>
            <strong class="mono">${escapeHtml(item.id)}</strong><br>
            ${pill(item.recommended_action || "manual", item.recommended_action === "open_review" ? "warning" : "danger")}
          </div>
          ${pill(worker?.status || "open", toneForStatus(worker?.status || "blocked"))}
        </div>
        <div>
          <strong>${escapeHtml(item.worker_name)}</strong> · <span class="mono">${escapeHtml(data?.run?.id || "selected-run")}</span><br>
          <span class="muted">${escapeHtml(item.reason || "Manual intervention required.")}</span>
        </div>
        <div class="meta-grid">
          <div><span class="label">Last Tool</span><div class="mono">${escapeHtml(item.last_tool_name || "—")}</div></div>
          <div><span class="label">Attempts</span><div class="mono">${escapeHtml(`${item.remediation_attempts || 0} remediation / ${item.restart_attempts || 0} restart`)}</div></div>
          <div><span class="label">Entry</span><div class="mono">${escapeHtml(shortText(item.relatedEntryId || "—", 24))}</div></div>
        </div>
        <pre>${escapeHtml(shortText(item.last_tool_error || "No tool error recorded.", 360))}</pre>
        <div class="approval-actions">
          ${relatedEntry ? `<button class="ghost" data-action="open-review" data-entry="${escapeHtml(item.relatedEntryId)}" type="button">Open Review</button>` : ""}
          ${restarting ? `<button type="button" disabled>Restarting…</button>` : ""}
          ${canRestart ? `<button data-action="restart-worker" data-worker="${escapeHtml(item.worker_name)}" type="button">Restart Worker</button>` : ""}
          <button class="ghost" data-action="reconcile" type="button">Reconcile</button>
          ${canArchive ? `<button data-action="archive" type="button">Archive</button>` : ""}
        </div>
      </article>
    `;
  }).join("") : `<div class="review-empty">No open interventions. If a worker stalls or the rescue loop exhausts its budget, it will appear here with the next recommended action.</div>`;

  runsPanel.innerHTML = runsNeedingAttention.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th>Run</th><th>Workdir</th><th>Status</th><th>Open Interventions</th><th>Pending Decisions</th></tr></thead>
      <tbody>${runsNeedingAttention.map((item) => `
        <tr>
          <td>
            <button
              class="ghost"
              data-action="select-run"
              data-data-dir="${escapeHtml(item.dataDirKey)}"
              data-run="${escapeHtml(item.id)}"
              type="button"
            ><strong>${escapeHtml(item.name)}</strong></button><br>
            <span class="mono">${escapeHtml(item.id)}</span>
          </td>
          <td><span class="mono">${escapeHtml(shortText(item.ownerDataDir || "—", 54))}</span></td>
          <td>${pill(item.status, toneForStatus(item.status))}</td>
          <td>${item.openInterventionCount || 0}</td>
          <td>${item.pendingApprovals || 0}</td>
        </tr>
      `).join("")}</tbody>
    </table></div>` : `<div class="review-empty">No tracked runs currently require intervention.</div>`;
}

function renderWorktreeMaster(data, selectedEntry) {
  const summaryLine = byId("worktree-summary");
  const panel = byId("worktree-panel");
  if (!summaryLine || !panel) return;
  const track = data.worktreeTrack || {};
  const summary = track.summary || {};
  const approvalCounts = {};
  for (const approval of data.approvals || []) {
    for (const entryId of approval.relatedEntryIds || []) {
      approvalCounts[entryId] = (approvalCounts[entryId] || 0) + 1;
    }
  }
  summaryLine.textContent =
    `Tracked ${summary.trackedWorkers || 0} worker(s) · ${summary.currentWorktrees || 0} current · ${summary.restoredWorktrees || 0} restored · ${summary.archivedSnapshots || 0} archived`;

  const groups = (track.tracks || []).map((group) => {
    const entries = [...(group.entries || [])].sort((left, right) => {
      const leftAt = Date.parse(left.recordedAt || "") || 0;
      const rightAt = Date.parse(right.recordedAt || "") || 0;
      return leftAt - rightAt;
    });
    const nodes = entries.map((entry) => {
      const selected = selectedEntry && selectedEntry.entryId === entry.entryId;
      const relatedGateCount = approvalCounts[entry.entryId] || 0;
      const tone = nodeTone(entry);
      const nodeTitle = entry.kind === "archived"
        ? (entry.archiveLabel || "Archive")
        : entry.kind === "restored"
          ? "Restored"
          : "Current";
      const label = entry.kind === "archived"
        ? (entry.archiveLabel || shortText(entry.archiveId || "archive", 18))
        : shortText(entry.relativePath || entry.workspacePath || entry.stageId || "-", 22);
      const statusLabel = entry.resultStatus
        ? entry.resultStatus
        : entry.previewUrl
          ? "preview ready"
          : "no report";
      return `
        <button class="tree-node kind-${escapeHtml(entry.kind)} ${selected ? "selected" : ""} ${relatedGateCount ? "contextual" : ""}" data-action="select-entry" data-entry="${escapeHtml(entry.entryId)}" type="button">
          <div class="tree-dot ${tone === "warning" ? "warning" : tone === "danger" ? "danger" : ""}"></div>
          <div class="tree-node-title">
            <span class="compact">${escapeHtml(nodeTitle)}</span>
            <strong>${escapeHtml(label)}</strong>
          </div>
          <div class="tree-node-meta">
            <span>${escapeHtml(statusLabel)}</span>
            <span>${escapeHtml(formatDate(entry.recordedAt || ""))}</span>
          </div>
          <div class="tree-node-badges">
            ${entry.previewUrl ? pill("preview", "ok") : ""}
            ${relatedGateCount ? pill(`${relatedGateCount} decision`, "warning") : ""}
            ${entry.resultStatus ? pill(entry.resultStatus, toneForStatus(entry.resultStatus)) : ""}
          </div>
        </button>
      `;
    }).join("");
    return `
      <div class="tree-group">
        <div class="tree-group-header">
          <strong>${escapeHtml(group.workerName)}</strong>
          <span class="compact">${group.entries.length} node(s)</span>
        </div>
        <div class="tree-timeline">
          <div class="tree-lane">
            <div class="tree-node-list">${nodes}</div>
          </div>
        </div>
      </div>
    `;
  }).join("");

  panel.innerHTML = groups || `<div class="review-empty">No tracked worktrees yet.</div>`;
}

function renderReviewPanel(data, selectedEntry) {
  const target = byId("review-panel");
  const contextLine = byId("review-context-line");
  if (!target || !contextLine) return;
  if (!selectedEntry) {
    contextLine.textContent = "Select a worktree node to inspect its preview, result, and actions.";
    target.innerHTML = `<div class="review-empty">No worktree node is selected yet.</div>`;
    return;
  }

  const worker = matchingWorker(data, selectedEntry);
  const archive = matchingArchive(data, selectedEntry);
  const reviewStatus = selectedEntry.resultStatus || "no-report";
  const canStop = worker && liveStatuses.has(worker.status);
  const canRestart = canRestartWorker(worker);
  const canArchive = canArchiveRun(data);
  const canRestartFromRun = canRestartRun(data);
  const canRestore = archive && archive.status === "approved";
  const canMerge = archive && archive.status === "approved";
  const stopping = worker ? isPendingAction("stop-worker", worker.worker_name) : false;
  const outputSnippet = selectedEntry.outputSnippet || (worker && worker.outputSnippet) || "";
  const surfaceSummary = selectedEntry.changedSurfaceSummary || "";
  const warnings = selectedEntry.warnings || [];
  const blockers = selectedEntry.blockers || [];

  contextLine.textContent = `${selectedEntry.workerName} · ${selectedEntry.kind} · ${selectedEntry.stageId || "stage"}`;

  target.innerHTML = `
    <div class="review-shell">
      <div class="review-hero">
        <div>
          <h3>${escapeHtml(selectedEntry.workerName)} · ${escapeHtml(selectedEntry.kind)}</h3>
          <div class="meta">
            ${archive ? `Archive ${escapeHtml(archive.label || archive.id || "archive")}` : `Stage ${escapeHtml(selectedEntry.stageId || "-")}`}<br>
            <span class="mono">${escapeHtml(selectedEntry.relativePath || selectedEntry.workspacePath || "-")}</span>
          </div>
        </div>
        <div class="review-badges">
          ${pill(selectedEntry.kind, selectedEntry.kind === "archived" ? "warning" : "ok")}
          ${pill(selectedEntry.status, toneForStatus(selectedEntry.status))}
          ${pill(reviewStatus, toneForStatus(reviewStatus))}
          ${selectedEntry.previewUrl ? pill("preview", "ok") : ""}
        </div>
      </div>
      <div class="review-grid">
        <div class="review-block">
          <h3>Summary</h3>
          ${surfaceSummary
            ? `<p>${escapeHtml(surfaceSummary)}</p>`
            : `<div class="review-empty">This worktree exists, but it has not reported a preview or result yet.</div>`}
        </div>
        <div class="review-block">
          <h3>Evidence</h3>
          <p>
            Preview: ${selectedEntry.previewUrl ? `<a href="${escapeHtml(selectedEntry.previewUrl)}" target="_blank" rel="noreferrer">${escapeHtml(selectedEntry.previewUrl)}</a>` : "—"}<br>
            Backend: ${selectedEntry.backendUrl ? `<span class="mono">${escapeHtml(selectedEntry.backendUrl)}</span>` : "—"}<br>
            Output: ${outputSnippet ? escapeHtml(shortText(outputSnippet, 220)) : "—"}<br>
            Report Source: <span class="mono">${escapeHtml((worker && worker.reportSource) || "unknown")}</span>
          </p>
          <div class="tree-node-badges" style="margin-top:10px; justify-content:flex-start;">
            ${warnings.length ? pill(`${warnings.length} warning`, "warning") : ""}
            ${blockers.length ? pill(`${blockers.length} blocker`, "danger") : ""}
          </div>
        </div>
      </div>
      <div class="review-block">
        <h3>Decision</h3>
        <p class="hint">Use the actions below to continue this worktree or move its archive forward.</p>
        <div class="review-grid">
          <div class="field-block">
            <span for="review-archive-label">Archive Label</span>
            <input id="review-archive-label" value="${escapeHtml(archive?.label || "dashboard-checkpoint")}">
          </div>
          <div class="field-block">
            <span for="review-archive-summary">Archive Summary</span>
            <textarea id="review-archive-summary" placeholder="Optional summary for archive creation."></textarea>
          </div>
        </div>
        ${warnings.length || blockers.length ? `
          <div class="review-grid" style="margin-top:12px;">
            <div class="review-block">
              <h3>Warnings</h3>
              ${warnings.length
                ? `<ul class="review-list">${warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
                : `<div class="review-empty">No warnings.</div>`}
            </div>
            <div class="review-block">
              <h3>Blockers</h3>
              ${blockers.length
                ? `<ul class="review-list">${blockers.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
                : `<div class="review-empty">No blockers.</div>`}
            </div>
          </div>
        ` : ""}
        <div class="approval-actions">
          ${stopping ? `<button type="button" disabled>Stopping…</button>` : ""}
          ${canStop ? `<button data-action="stop-worker" data-worker="${escapeHtml(selectedEntry.workerName)}" type="button">Stop Worker</button>` : ""}
          <button class="ghost" data-action="reconcile" type="button">Reconcile</button>
          ${!canStop && canArchive ? `<button data-action="archive" type="button">Create Archive</button>` : ""}
          ${canRestart && canRestartFromRun ? `<button class="ghost" data-action="restart-worker" data-worker="${escapeHtml(selectedEntry.workerName)}" type="button">Restart Worker</button>` : ""}
          ${canRestore ? `<button class="ghost" data-action="restore-archive" data-archive="${escapeHtml(archive.id)}" type="button">Restore Archive</button>` : ""}
          ${canMerge ? `<button class="ghost" data-action="merge-archive" data-archive="${escapeHtml(archive.id)}" type="button">Merge Archive</button>` : ""}
        </div>
      </div>
    </div>
  `;
}

function renderApprovals(data, selectedEntry) {
  const rail = byId("approvals-panel");
  const contextLine = byId("approval-context-line");
  if (!rail || !contextLine) return;
  const approvals = [...(data.approvals || [])].sort((left, right) => {
    const leftPriority = approvalPriority(left, selectedEntry);
    const rightPriority = approvalPriority(right, selectedEntry);
    if (leftPriority !== rightPriority) return leftPriority - rightPriority;
    return (Date.parse(right.created_at || "") || 0) - (Date.parse(left.created_at || "") || 0);
  });
  if (!approvals.length) {
    contextLine.textContent = "No pending decisions for this run.";
    rail.innerHTML = `<div class="review-empty">No pending decisions.</div>`;
    return;
  }

  contextLine.textContent = selectedEntry
    ? `Showing decisions related to ${selectedEntry.workerName} · ${selectedEntry.kind} first.`
    : "Showing run-wide pending decisions.";

  rail.innerHTML = approvals.map((approval) => {
    const inContext = selectedEntry && (approval.relatedEntryIds || []).includes(selectedEntry.entryId);
    const expanded = inContext || state.expandedGateId === approval.id;
    return `
      <div class="approval-card ${inContext ? "in-context" : ""}">
        <div class="approval-head">
          <div>
            <strong class="mono">${escapeHtml(approval.id)}</strong><br>
            ${pill(approval.gate_type, toneForStatus(approval.gate_type))}
          </div>
          ${inContext ? pill("in context", "ok") : ""}
        </div>
        <div class="approval-context">
          Target <span class="mono">${escapeHtml(approval.target_id)}</span><br>
          ${escapeHtml(approvalContextSummary(approval))}<br>
          <span class="mono">${escapeHtml(formatDate(approval.created_at))}</span>
        </div>
        ${expanded ? `
          <div class="field-block">
            <span for="approval-feedback-${escapeHtml(approval.id)}">Feedback</span>
            <textarea id="approval-feedback-${escapeHtml(approval.id)}" placeholder="Optional feedback for this gate.">${escapeHtml(approval.feedback || "")}</textarea>
          </div>
          <div class="approval-actions">
            <button data-action="approve-gate" data-gate="${escapeHtml(approval.id)}" type="button">Approve</button>
            <button class="ghost" data-action="reject-gate" data-gate="${escapeHtml(approval.id)}" type="button">Reject</button>
          </div>
        ` : `
          <div class="approval-actions">
            <button class="ghost" data-action="expand-gate" data-gate="${escapeHtml(approval.id)}" type="button">Review decision</button>
          </div>
        `}
      </div>
    `;
  }).join("");
}

function renderArchives(data, selectedEntry) {
  const target = byId("archives-panel");
  if (!target) return;
  const rows = data.archives.map((archive) => {
    const contextual = selectedEntry && selectedEntry.archiveId === archive.id;
    return `
      <div class="archive-card ${contextual ? "contextual" : ""}">
        <div style="display:flex; justify-content:space-between; gap:10px; align-items:flex-start; flex-wrap:wrap;">
          <div>
            <strong class="mono" title="${escapeHtml(archive.id)}">${escapeHtml(shortText(archive.id, 26))}</strong><br>
            <span class="muted">${escapeHtml(archive.label || "Unlabeled archive")}</span>
          </div>
          ${pill(archive.status, toneForStatus(archive.status))}
        </div>
        <div class="archive-meta">
          <div><span class="label">Stage</span><div class="mono">${escapeHtml(archive.stage_id)}</div></div>
          <div><span class="label">Created</span><div class="mono">${escapeHtml(formatDate(archive.created_at || ""))}</div></div>
        </div>
        <div class="archive-actions">
          ${archive.status === "approved"
            ? `<button data-action="restore-archive" data-archive="${escapeHtml(archive.id)}" type="button">Restore</button><button class="ghost" data-action="merge-archive" data-archive="${escapeHtml(archive.id)}" type="button">Merge</button>`
            : `<span class="muted">Awaiting approval before restore or merge.</span>`}
        </div>
      </div>
    `;
  });
  target.innerHTML = data.archives.length
    ? `<div class="archive-grid">${rows.join("")}</div>`
    : `<div class="review-empty">No archives yet.</div>`;
}

function renderEvents(selectedEntry) {
  const panel = byId("events-panel");
  if (!panel) return;
  const nodeFilter = byId("events-node-filter");
  const runFilter = byId("events-run-filter");
  const levelAll = byId("events-level-all");
  const levelInfo = byId("events-level-info");
  const levelWarning = byId("events-level-warning");
  const levelError = byId("events-level-error");
  const scoped = state.eventScope === "run"
    ? state.recentEvents
    : state.recentEvents.filter((event) => eventMatchesEntry(event, selectedEntry));
  const filtered = state.eventLevel === "all"
    ? scoped
    : scoped.filter((event) => eventLevel(event) === state.eventLevel);
  if (nodeFilter) nodeFilter.className = state.eventScope === "node" ? "active-toggle" : "subtle";
  if (runFilter) runFilter.className = state.eventScope === "run" ? "active-toggle" : "subtle";
  if (levelAll) levelAll.className = state.eventLevel === "all" ? "active-toggle" : "subtle";
  if (levelInfo) levelInfo.className = state.eventLevel === "info" ? "active-toggle" : "subtle";
  if (levelWarning) levelWarning.className = state.eventLevel === "warning" ? "active-toggle" : "subtle";
  if (levelError) levelError.className = state.eventLevel === "error" ? "active-toggle" : "subtle";
  const rows = filtered.map((event) => {
    const described = describeEvent(event);
    const level = eventLevel(event);
    return `
      <div class="event-row">
        <div class="event-row-header">
          <div>
            <strong class="mono">${escapeHtml(event.id)}</strong>
            ${pill(event.event_type, toneForStatus(event.event_type))}
            ${pill(level, level === "error" ? "danger" : level === "warning" ? "warning" : "ok")}
          </div>
          <div class="compact">${escapeHtml(formatDate(event.timestamp))}</div>
        </div>
        <div class="event-row-body">
          <div class="event-row-summary">${escapeHtml(described.summary)}</div>
          <div class="event-row-detail">${escapeHtml(described.detail)}</div>
        </div>
      </div>
    `;
  });
  panel.innerHTML = rows.length
    ? `<div class="event-list">${rows.join("")}</div>`
    : `<div class="review-empty">No ${state.eventLevel === "all" ? "" : `${state.eventLevel} `}events in the current ${state.eventScope} scope.</div>`;
}

function setRunSummaryLine(data) {
  const summaryLine = byId("summary-line");
  if (!summaryLine) return;
  summaryLine.textContent =
    `${data.run.name || data.run.id} · ${data.workers.length} worktree(s) · ${data.approvals.length} decision(s) waiting · ${data.run.openInterventionCount || 0} intervention(s)`;
}

function renderSnapshot(data, signature = snapshotSignatureFor(data)) {
  state.snapshot = data;
  state.snapshotSignature = signature;
  const selectedEntry = ensureSelectedEntry(data);
  renderGlobalCards();
  renderActivePage();
  renderReviewTabs();
  renderPickerPanel();
  renderRunHomeHeader(data);
  renderRunInterventionStrip(data);
  renderFeatureQueue(data);
  renderBatchReview(data);
  renderDaemonOverview();
  renderWorkspaceEntryButtons(data);
  renderDrawer();
  renderRunOverview(data);
  renderInterventionsPage(data);
  renderDataDirs();
  renderProcesses();
  renderWorktreeMaster(data, selectedEntry);
  renderReviewPanel(data, selectedEntry);
  renderSelectedDetailPanel(data, selectedEntry);
  renderApprovals(data, selectedEntry);
  renderArchives(data, selectedEntry);
  renderWorkers(data, selectedEntry);
  renderConstraints(data);
  renderEvents(selectedEntry);
  rememberSelection();
  setRunSummaryLine(data);
}

function renderSnapshotIfChanged(data, { force = false } = {}) {
  const signature = snapshotSignatureFor(data);
  if (!force && signature === state.snapshotSignature) return false;
  renderSnapshot(data, signature);
  return true;
}

async function fetchGlobalState() {
  const [daemon, dataDirs, processes, runs] = await Promise.all([
    fetchJson("/api/daemon/status"),
    fetchJson("/api/data-dirs"),
    fetchJson("/api/processes"),
    fetchJson("/api/runs"),
  ]);
  return { daemon, dataDirs, processes, runs };
}

function applyGlobalState({ daemon, dataDirs, processes, runs }) {
  state.daemon = daemon;
  state.dataDirs = dataDirs;
  state.processes = processes;
  state.runs = runs;
  const content = byId("content");
  if (content) content.classList.remove("hidden");
  renderGlobalCards();
  renderPickerPanel();
  renderDataDirs();
  renderProcesses();
  renderDaemonOverview();
}

async function loadGlobal(preferredDataDir = "", preferredRunId = "", preferredView = "", preferredEntryId = "") {
  const globalState = await fetchGlobalState();
  const { dataDirs, runs } = globalState;
  applyGlobalState(globalState);

  if (!runs.length) {
    closeEventSource();
    clearRunSnapshot();
    state.selectedRunId = "";
    state.selectedDataDirKey = "";
    state.activePage = state.activePage === "control-plane" ? "control-plane" : "picker";
    renderWorkspaceEntryButtons(null);
    renderDrawer();
    renderActivePage();
    renderReviewTabs();
    renderInterventionsPage(null);
    const summaryLine = byId("summary-line");
    const connectionPill = byId("connection-pill");
    if (summaryLine) summaryLine.textContent = "No runs yet. Create a run to start from a workdir.";
    if (connectionPill) {
      connectionPill.className = "pill warning";
      connectionPill.textContent = "Idle";
    }
    rememberSelection();
    return;
  }

  const stayOnPicker = (preferredView || state.activePage) === "picker";
  if (stayOnPicker) {
    closeEventSource();
    clearRunSnapshot();
    state.selectedRunId = "";
    state.selectedDataDirKey = preferredDataDir && dataDirs.some((item) => item.dataDirKey === preferredDataDir)
      ? preferredDataDir
      : (state.selectedDataDirKey && dataDirs.some((item) => item.dataDirKey === state.selectedDataDirKey) ? state.selectedDataDirKey : "");
    renderWorkspaceEntryButtons(null);
    renderDrawer();
    renderActivePage();
    renderReviewTabs();
    renderInterventionsPage(null);
    const summaryLine = byId("summary-line");
    const connectionPill = byId("connection-pill");
    if (summaryLine) summaryLine.textContent = "Choose a workdir and run to start from worktrees.";
    if (connectionPill) {
      connectionPill.className = "pill";
      connectionPill.textContent = "Ready";
    }
    rememberSelection();
    return;
  }

  const preferredMatch = preferredRunId
    ? runs.find((item) => item.id === preferredRunId && (!preferredDataDir || item.dataDirKey === preferredDataDir))
    : null;
  const smartMatch = !preferredMatch && !preferredRunId ? pickSmartRun(runs) : null;
  const currentMatch = state.selectedRunId
    ? runs.find((item) => item.id === state.selectedRunId && (!state.selectedDataDirKey || item.dataDirKey === state.selectedDataDirKey))
    : null;

  const nextRunRecord = preferredMatch || currentMatch || smartMatch || null;
  if (!nextRunRecord) {
    closeEventSource();
    clearRunSnapshot();
    state.selectedRunId = "";
    state.selectedDataDirKey = preferredDataDir && dataDirs.some((item) => item.dataDirKey === preferredDataDir)
      ? preferredDataDir
      : (state.selectedDataDirKey && dataDirs.some((item) => item.dataDirKey === state.selectedDataDirKey) ? state.selectedDataDirKey : "");
    state.activePage = state.activePage === "control-plane" ? "control-plane" : "picker";
    renderWorkspaceEntryButtons(null);
    renderDrawer();
    renderActivePage();
    renderReviewTabs();
    renderInterventionsPage(null);
    const summaryLine = byId("summary-line");
    const connectionPill = byId("connection-pill");
    if (summaryLine) summaryLine.textContent = "Choose a workdir and run to start from worktrees.";
    if (connectionPill) {
      connectionPill.className = "pill";
      connectionPill.textContent = "Ready";
    }
    rememberSelection();
    return;
  }

  state.selectedDataDirKey = nextRunRecord.dataDirKey;
  state.selectedRunId = nextRunRecord.id;
  state.selectedEntryId = preferredEntryId || "";
  if (preferredView) state.activePage = preferredView;
  await connectRun(nextRunRecord.dataDirKey, nextRunRecord.id, preferredView, preferredEntryId);
}

async function waitForWorkerEntry(workerName, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await refreshAll({ forceRunSnapshot: true });
    const entryId = latestEntryIdForWorker(state.snapshot, workerName);
    if (entryId) return entryId;
    await new Promise((resolve) => window.setTimeout(resolve, 500));
  }
  return "";
}

async function refreshRecentEvents({ forceRender = false } = {}) {
  const events = await fetchJson(scopedRecentEventsUrl());
  const signature = serializeState(events);
  state.recentEvents = events;
  const changed = forceRender || signature !== state.recentEventsSignature;
  state.recentEventsSignature = signature;
  if (changed) renderEvents(ensureSelectedEntry(state.snapshot));
}

async function connectRun(dataDirKeyValue, runId, preferredView = "", preferredEntryId = "") {
  closeEventSource();
  state.selectedDataDirKey = dataDirKeyValue;
  state.selectedRunId = runId;
  if (preferredView) state.activePage = preferredView;
  if (preferredEntryId) state.selectedEntryId = preferredEntryId;
  const pillEl = byId("connection-pill");
  if (pillEl) {
    pillEl.className = "pill";
    pillEl.textContent = "Loading";
  }
  const snapshot = await fetchJson(`/api/data-dirs/${encodeURIComponent(dataDirKeyValue)}/runs/${encodeURIComponent(runId)}`);
  if (state.activePage === "picker") state.activePage = "workspace";
  renderSnapshotIfChanged(snapshot, { force: true });
  await refreshRecentEvents({ forceRender: true });
  rememberSelection();
  syncUrlState();
  state.eventSource = new EventSource(scopedEventsUrl());
  state.eventSource.onmessage = async (event) => {
    if (pillEl) {
      pillEl.className = "pill";
      pillEl.textContent = "Streaming";
    }
    const snapshotChanged = renderSnapshotIfChanged(JSON.parse(event.data));
    const reviewEventsVisible = state.activePage === "review" && state.reviewTab === "events";
    if (snapshotChanged || reviewEventsVisible) {
      await refreshRecentEvents({ forceRender: snapshotChanged });
    }
  };
  state.eventSource.onerror = () => {
    if (pillEl) {
      pillEl.className = "pill warning";
      pillEl.textContent = "Reconnecting";
    }
  };
}

function hasRunContext() {
  return Boolean(state.selectedDataDirKey && state.selectedRunId && state.activePage !== "picker");
}

async function refreshActiveRun({ forceSnapshot = false } = {}) {
  const globalState = await fetchGlobalState();
  applyGlobalState(globalState);
  const currentRun = state.runs.find(
    (item) => item.id === state.selectedRunId && item.dataDirKey === state.selectedDataDirKey,
  );
  if (!currentRun) {
    await loadGlobal(state.selectedDataDirKey, state.selectedRunId, state.activePage, state.selectedEntryId);
    return;
  }
  if (!state.snapshot || forceSnapshot || !state.eventSource) {
    const snapshot = await fetchJson(scopedRunUrl());
    const snapshotChanged = renderSnapshotIfChanged(snapshot, { force: forceSnapshot || !state.snapshot });
    await refreshRecentEvents({ forceRender: forceSnapshot || snapshotChanged || !state.recentEvents.length });
    rememberSelection();
    syncUrlState();
    return;
  }
  setRunSummaryLine(state.snapshot);
}

async function refreshAll({ forceRunSnapshot = false } = {}) {
  if (hasRunContext()) {
    await refreshActiveRun({ forceSnapshot: forceRunSnapshot });
    return;
  }
  await loadGlobal(state.selectedDataDirKey, state.selectedRunId, state.activePage, state.selectedEntryId);
}

async function waitForWorkerTerminal(workerName, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await refreshAll({ forceRunSnapshot: true });
    const worker = (state.snapshot?.workers || []).find((item) => item.worker_name === workerName);
    if (!worker || !liveStatuses.has(worker.status)) return;
    await new Promise((resolve) => window.setTimeout(resolve, 600));
  }
}

async function performAction(kind, value) {
  if (!state.selectedDataDirKey || !state.selectedRunId) return;
  let url = "";
  let payload = {};
  if (kind === "stop-worker") {
    url = `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/workers/${encodeURIComponent(value)}/stop`;
  } else if (kind === "restart-worker") {
    url = `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/workers/${encodeURIComponent(value)}/restart`;
  } else if (kind === "approve-gate") {
    url = `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/gates/${encodeURIComponent(value)}/approve`;
    payload = { actor: readActor(), feedback: gateFeedback(value) };
  } else if (kind === "reject-gate") {
    url = `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/gates/${encodeURIComponent(value)}/reject`;
    payload = { actor: readActor(), feedback: gateFeedback(value) };
  } else if (kind === "restore-archive") {
    url = `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/archives/${encodeURIComponent(value)}/restore`;
    payload = { actor: readActor() };
  } else if (kind === "merge-archive") {
    url = `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/merge-request`;
    payload = { actor: readActor(), archiveId: value };
  } else if (kind === "merge-batch") {
    url = `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/merge-request`;
    payload = { actor: readActor(), batchId: value };
  } else if (kind === "promote-batch") {
    url = `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/promote-request`;
    payload = { actor: readActor(), batchId: value };
  } else if (kind === "reconcile") {
    url = `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/reconcile`;
  } else if (kind === "archive") {
    url = `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/archives`;
    payload = { actor: readActor(), ...archiveFormPayload() };
  } else {
    return;
  }
  setPendingAction(kind, value, true);
  if (state.snapshot) renderSnapshot(state.snapshot);
  try {
    await postJson(url, payload);
    if (kind === "stop-worker") {
      await waitForWorkerTerminal(value);
    }
    await refreshAll({ forceRunSnapshot: true });
  } catch (error) {
    window.alert(error.message || String(error));
  } finally {
    setPendingAction(kind, value, false);
    if (state.snapshot) renderSnapshot(state.snapshot);
  }
}

async function submitNewRun(event) {
  event.preventDefault();
  const dataDirSelect = byId("new-run-data-dir-select");
  const dataDirManual = byId("new-run-data-dir-manual");
  const dataDirError = byId("new-run-data-dir-error");
  const newRunRepo = byId("new-run-repo");
  const newRunName = byId("new-run-name");
  const newRunDescription = byId("new-run-description");
  const newRunDirection = byId("new-run-direction");
  const newRunIntegrationRef = byId("new-run-integration-ref");
  const newRunMaxFeatures = byId("new-run-max-features");
  const newRunProfile = byId("new-run-profile");
  const newRunSpec = byId("new-run-spec");
  const newRunRules = byId("new-run-rules");
  const newRunPlan = byId("new-run-plan");
  if (!dataDirSelect || !dataDirManual || !dataDirError || !newRunRepo || !newRunName || !newRunDescription || !newRunDirection || !newRunIntegrationRef || !newRunMaxFeatures || !newRunProfile || !newRunSpec || !newRunRules || !newRunPlan) return;
  const payload = {
    dataDirKey: dataDirSelect.value !== "__manual__" ? dataDirSelect.value : "",
    dataDir: dataDirSelect.value === "__manual__" ? dataDirManual.value.trim() : "",
    repo: newRunRepo.value.trim(),
    name: newRunName.value.trim(),
    description: newRunDescription.value.trim(),
    direction: newRunDirection.value.trim(),
    integrationRef: newRunIntegrationRef.value.trim(),
    maxActiveFeatures: Number(newRunMaxFeatures.value || 2),
    projectProfile: newRunProfile.value,
    specContent: newRunSpec.value.trim(),
    rulesContent: newRunRules.value.trim(),
    initialPlan: newRunPlan.value.trim(),
    author: readActor(),
  };
  if (!payload.repo) {
    window.alert("New Run requires a local repo path.");
    return;
  }
  if (!payload.name) {
    window.alert("New Run requires a run name.");
    return;
  }
  if (!payload.initialPlan) {
    window.alert("New Run requires an initial plan.");
    return;
  }
  if (!payload.dataDirKey && !payload.dataDir) {
    dataDirError.textContent = "Workdir must point to a .branchclaw folder.";
    dataDirError.classList.remove("hidden");
    return;
  }
  if (!payload.dataDirKey) {
    if (!payload.dataDir.startsWith("/")) {
      dataDirError.textContent = "Workdir must be an absolute path to a .branchclaw folder.";
      dataDirError.classList.remove("hidden");
      return;
    }
    if (!payload.dataDir.endsWith("/.branchclaw") && !payload.dataDir.endsWith(".branchclaw")) {
      dataDirError.textContent = "Workdir must point directly to the .branchclaw folder, not its parent directory.";
      dataDirError.classList.remove("hidden");
      return;
    }
  }
  dataDirError.textContent = "";
  dataDirError.classList.add("hidden");
  const submit = byId("new-run-submit");
  if (!submit) return;
  submit.disabled = true;
  submit.textContent = "Creating…";
  try {
    const created = await postJson("/api/runs", payload);
    closeDrawer();
    navigateToPage("workspace", {
      dataDirKey: created.dataDirKey || "",
      runId: created.runId || "",
      entryId: "",
    });
  } catch (error) {
    window.alert(error.message || String(error));
  } finally {
    submit.disabled = false;
    submit.textContent = "Create Run";
  }
}

async function submitNewWorkspace(event) {
  event.preventDefault();
  if (!state.selectedDataDirKey || !state.selectedRunId) {
    window.alert("Select a run before adding a worktree.");
    return;
  }
  const featureId = byId("new-workspace-feature-id");
  const workerName = byId("new-workspace-name");
  const task = byId("new-workspace-task");
  const backend = byId("new-workspace-backend");
  const command = byId("new-workspace-command");
  const skipPermissions = byId("new-workspace-skip-permissions");
  if (!featureId || !workerName || !task || !backend || !command || !skipPermissions) return;
  const payload = {
    featureId: featureId.value.trim(),
    workerName: workerName.value.trim(),
    task: task.value.trim(),
    backend: backend.value,
    command: command.value.trim(),
    skipPermissions: skipPermissions.checked,
  };
  if (!payload.workerName) {
    window.alert("Add Worktree requires a worker name.");
    return;
  }
  if (!payload.task) {
    window.alert("Add Worktree requires a task.");
    return;
  }
  const submit = byId("new-workspace-submit");
  if (!submit) return;
  submit.disabled = true;
  submit.textContent = "Creating…";
  try {
    const created = await postJson(
      `/api/data-dirs/${encodeURIComponent(state.selectedDataDirKey)}/runs/${encodeURIComponent(state.selectedRunId)}/workers`,
      payload,
    );
    closeDrawer();
    state.activePage = "workspace";
    const entryId = await waitForWorkerEntry(created.workerName || payload.workerName);
    if (entryId) state.selectedEntryId = entryId;
    if (state.snapshot) {
      renderSnapshot(state.snapshot);
      renderEvents(ensureSelectedEntry(state.snapshot));
    }
  } catch (error) {
    window.alert(error.message || String(error));
  } finally {
    submit.disabled = false;
    submit.textContent = "Add Worktree";
  }
}

document.addEventListener("click", (event) => {
  const origin = event.target;
  if (!(origin instanceof HTMLElement)) return;
  const target = origin.closest("[data-action]");
  if (!(target instanceof HTMLElement)) return;
  const action = target.dataset.action;
  if (!action) return;
  if (action === "show-page") {
    navigateToPage(target.dataset.page || "picker").catch((error) => window.alert(error.message || String(error)));
    return;
  }
  if (action === "open-new-run") {
    if (target.dataset.dataDir) state.selectedDataDirKey = target.dataset.dataDir;
    openDrawer("new-run");
    return;
  }
  if (action === "open-new-workspace") {
    openDrawer("new-workspace");
    return;
  }
  if (action === "go-home") {
    navigateToPage("picker", { runId: "", entryId: "" }).catch((error) => window.alert(error.message || String(error)));
    return;
  }
  if (action === "select-run") {
    navigateToPage("workspace", {
      dataDirKey: target.dataset.dataDir || "",
      runId: target.dataset.run || "",
      entryId: "",
    }).catch((error) => window.alert(error.message || String(error)));
    return;
  }
  if (action === "close-drawer") {
    closeDrawer();
    return;
  }
  if (action === "open-review") {
    navigateToPage("review", { entryId: target.dataset.entry || "" }).catch((error) => window.alert(error.message || String(error)));
    return;
  }
  if (action === "expand-gate") {
    state.expandedGateId = target.dataset.gate || "";
    if (state.snapshot) renderApprovals(state.snapshot, ensureSelectedEntry(state.snapshot));
    return;
  }
  if (action === "select-entry") {
    navigateToPage("review", { entryId: target.dataset.entry || "" }).catch((error) => window.alert(error.message || String(error)));
    return;
  }
  if (action === "show-review-tab") {
    state.reviewTab = reviewTabs.includes(target.dataset.tab || "") ? target.dataset.tab : "activity";
    renderReviewTabs();
    syncUrlState();
    return;
  }
  if (action === "events-node") {
    state.eventScope = "node";
    renderEvents(ensureSelectedEntry(state.snapshot));
    return;
  }
  if (action === "events-run") {
    state.eventScope = "run";
    renderEvents(ensureSelectedEntry(state.snapshot));
    return;
  }
  if (action === "set-events-level") {
    state.eventLevel = target.dataset.level || "all";
    renderEvents(ensureSelectedEntry(state.snapshot));
    return;
  }
  performAction(action, target.dataset.worker || target.dataset.gate || target.dataset.archive || target.dataset.batch || "");
});

bind("refresh-button", "click", () => {
  refreshAll({ forceRunSnapshot: true }).catch((error) => window.alert(error.message));
});
bind("home-button", "click", () => {
  navigateToPage("picker", { runId: "", entryId: "" }).catch((error) => window.alert(error.message || String(error)));
});
bind("control-plane-button", "click", () => {
  navigateToPage("control-plane").catch((error) => window.alert(error.message || String(error)));
});
bind("new-run-button", "click", () => { openDrawer("new-run"); });
bind("new-run-data-dir-select", "change", syncNewRunDataDirMode);
bind("new-run-form", "submit", submitNewRun);
bind("new-workspace-form", "submit", submitNewWorkspace);

const initialUrlState = parseUrlState();
state.activePage = currentPageId();
const initialContainer = mainPagesContainer();
if (initialContainer) {
  pageTemplateCache.set(state.activePage, initialContainer.innerHTML);
}
for (const page of pageKinds) {
  if (!pageTemplateCache.has(page)) {
    const markup = templateMarkupFromDom(page);
    if (markup) pageTemplateCache.set(page, markup);
  }
}
const requestedInitialPage = pageKinds.includes(initialUrlState.view || "") ? initialUrlState.view : state.activePage;
if (requestedInitialPage !== state.activePage) {
  mountPage(requestedInitialPage)
    .then(() => {
      state.activePage = requestedInitialPage;
      if (initialUrlState.entryId) {
        state.selectedEntryId = initialUrlState.entryId;
      }
      return loadGlobal(
        initialUrlState.dataDirKey,
        initialUrlState.runId,
        state.activePage,
        initialUrlState.entryId,
      );
    })
    .catch((error) => {
      const summaryLine = byId("summary-line");
      const connectionPill = byId("connection-pill");
      if (summaryLine) summaryLine.textContent = error.message;
      if (connectionPill) {
        connectionPill.className = "pill danger";
        connectionPill.textContent = "Error";
      }
    });
} else {
  if (initialUrlState.entryId) {
    state.selectedEntryId = initialUrlState.entryId;
  }
  loadGlobal(
    initialUrlState.dataDirKey,
    initialUrlState.runId,
    state.activePage,
    initialUrlState.entryId,
  ).catch((error) => {
    const summaryLine = byId("summary-line");
    const connectionPill = byId("connection-pill");
    if (summaryLine) summaryLine.textContent = error.message;
    if (connectionPill) {
      connectionPill.className = "pill danger";
      connectionPill.textContent = "Error";
    }
  });
}
window.addEventListener("popstate", () => {
  const urlState = parseUrlState();
  const nextPage = pageKinds.includes(urlState.view) ? urlState.view : currentPageId();
  navigateToPage(nextPage, {
    dataDirKey: urlState.dataDirKey,
    runId: urlState.runId,
    entryId: urlState.entryId,
  }, { replace: true }).catch((error) => window.alert(error.message || String(error)));
});
setInterval(() => { refreshAll().catch(() => {}); }, 10000);
