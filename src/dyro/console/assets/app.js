const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const TOKEN_KEY = "dyro.console.bearer";
const state = { bearer: "", etags: new Map(), timer: null, focus: "", partial: false, surfaces: [], system: null };
const HEALTH_LABELS = { healthy: "健康", degraded: "需关注", unavailable: "不可用" };
const FRESHNESS_LABELS = { fresh: "新鲜", partial: "部分可用", stale: "待更新" };
const AVAILABILITY_LABELS = { available: "可用", unavailable: "不可用" };
const LINE_KIND_LABELS = { line: "开发线", hotfix: "热修线" };
const TASK_STATUS_LABELS = {
  backlog: "待办",
  assigned: "已分配",
  in_progress: "进行中",
  waiting_answer: "待回答",
  review: "复核中",
  review_pending_signoff: "待签核",
  done: "已完成",
  failed: "失败",
};
const TASK_STATUS_ORDER = [
  "backlog",
  "assigned",
  "in_progress",
  "waiting_answer",
  "review",
  "review_pending_signoff",
  "done",
  "failed",
];
const OPERATOR_STATE_LABELS = {
  active: "进行中",
  paused: "已暂停",
  completed: "已完成",
  repair_required: "需要修复",
};
const PROOF_INSPECTION_LABELS = { not_inspected: "摘要未核验", inspected: "已独立检查" };
const PROOF_KIND_LABELS = {
  gate_log: "门禁日志",
  review_verdict: "复核结论",
  signoff: "外部签核",
  integration_heads: "集成 HEAD",
  action_receipt: "动作回执",
  trigger_observation: "触发观察",
};
const PROOF_STATUS_LABELS = {
  live: "检查仍有效",
  decayed: "已衰减",
  inconclusive: "无法判定",
  revoked: "已撤销",
};
const PROOF_LIVE_LABELS = {
  review_verdict: "复核仍绑当前 HEAD",
  signoff: "签核仍绑当前 HEAD",
  trigger_observation: "探测窗口未到期",
  gate_log: "门禁字节仍匹配",
  integration_heads: "集成 HEAD 仍在祖先链",
  action_receipt: "动作回执仍有效",
};
const PROOF_DECAY_LABELS = {
  review_acceptance: "复核绑定已失效",
  external_signoff: "外部签核已失效",
  dependency_integrated: "依赖集成已失效",
  gate_bytes: "门禁字节已失效",
  next_probe_at: "下次探测已到期",
  still_bound: "谓词仍成立",
  predicate_inconclusive: "谓词无法判定",
};
const ERROR_LABELS = {
  LOCAL_READ_UNAVAILABLE: "本地状态暂时不可读取",
  OVERVIEW_UNAVAILABLE: "工作区概览暂时不可读取",
  WORKSPACE_UNAVAILABLE: "工作区当前不可用",
  SESSION_REJECTED: "本地会话未建立",
};
const UPDATE_KIND_LABELS = {
  none: "无已缓存更新",
  patch: "有补丁更新",
  minor: "有次版本更新",
  major: "有主版本更新",
};

const $ = (id) => document.getElementById(id);

function setStatus(message, error = false) {
  const node = $("session-status");
  node.textContent = message;
  node.classList.toggle("error", error);
}

function text(value) {
  return typeof value === "string" ? value : "";
}

function count(value) {
  return Number.isInteger(value) && value >= 0 ? value : 0;
}

function workspaceCount(summary, key) {
  if (!summary || text(summary.availability) !== "available") return "—";
  const value = summary[key];
  return Number.isInteger(value) && value >= 0 ? String(value) : "—";
}

function unavailableWorkspaceCount(workspaces) {
  if (!Array.isArray(workspaces)) return 0;
  return workspaces.filter((summary) => text(summary.availability) !== "available").length;
}

function displayLabel(value, labels) {
  const raw = text(value);
  return labels[raw] || raw || "未提供";
}

function taskIntegrationLabel(state) {
  // 摘要路径只能是 not_inspected。写成「已集成」会把未核验摘要伪装成 merge 放行。
  return text(state) === "not_inspected" ? "摘要未核验集成" : "未提供";
}

function describeTask(task) {
  const status = displayLabel(task && task.status, TASK_STATUS_LABELS);
  const integration = taskIntegrationLabel(task && task.integration_state);
  const blocked = Array.isArray(task && task.blocked_on) && task.blocked_on.length
    ? `阻塞于 ${task.blocked_on.map((item) => text(item)).filter(Boolean).join("、")}`
    : "";
  return blocked ? `${status} · ${integration} · ${blocked}` : `${status} · ${integration}`;
}

function proofStatusLabel(kind, status) {
  const raw = text(status);
  if (raw === "live") return displayLabel(kind, PROOF_LIVE_LABELS);
  return displayLabel(raw, PROOF_STATUS_LABELS);
}

function userError(value) {
  const code = text(value);
  return ERROR_LABELS[code] || "本地状态暂时不可读取";
}

function element(name, value = "") {
  const node = document.createElement(name);
  if (value) node.textContent = value;
  return node;
}

function consumeFragment() {
  const raw = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : "";
  const values = new URLSearchParams(raw);
  const bootstrap = values.get("bootstrap");
  const workspace = values.get("workspace");
  state.focus = workspace && SAFE_ID.test(workspace) ? workspace : "";
  const safeRoute = state.focus ? `#workspace=${state.focus}` : "";
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${safeRoute}`);
  return bootstrap && bootstrap.length <= 256 ? bootstrap : "";
}

async function exchange(bootstrap) {
  const response = await fetch("/api/v1/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bootstrap }),
    cache: "no-store",
    credentials: "omit",
  });
  const body = await response.json().catch(() => null);
  if (!response.ok || !body || typeof body.bearer !== "string") {
    throw new Error("SESSION_REJECTED");
  }
  state.bearer = body.bearer;
  sessionStorage.setItem(TOKEN_KEY, state.bearer);
}

async function request(path, key) {
  const headers = { Authorization: `Bearer ${state.bearer}` };
  const etag = state.etags.get(key);
  if (etag) headers["If-None-Match"] = etag;
  const response = await fetch(path, { headers, cache: "no-store", credentials: "omit" });
  if (response.status === 304) return null;
  const body = await response.json().catch(() => null);
  if (response.status === 401) throw new Error("SESSION_EXPIRED");
  if (!response.ok || !body) {
    const code = text(body && body.error && body.error.code) || "LOCAL_READ_UNAVAILABLE";
    throw new Error(code);
  }
  const received = response.headers.get("ETag");
  if (received) state.etags.set(key, received);
  return body;
}

function expireSession() {
  state.bearer = "";
  state.etags.clear();
  window.clearTimeout(state.timer);
  state.timer = null;
  sessionStorage.removeItem(TOKEN_KEY);
  setStatus("本地会话已过期；请重新运行 dyro console。", true);
}

function addBadge(parent, label, level = "") {
  const badge = element("span", label);
  badge.className = "badge";
  if (level) badge.dataset.level = level;
  parent.append(badge);
}

function workspaceAttention(summary) {
  const attention = summary.attention_counts || {};
  if (count(attention.repair_required)) return 0;
  if (count(attention.needs_user)) return 1;
  if (count(attention.ready)) return 2;
  if (count(attention.waiting)) return 3;
  if (count(attention.paused)) return 4;
  return 5;
}

function priorityWorkspace(workspaces) {
  const focused = workspaces.find((summary) => text(summary.alias) === state.focus);
  if (focused && text(focused.recommendation && focused.recommendation.command)) return focused;
  return [...workspaces]
    .sort((left, right) => workspaceAttention(left) - workspaceAttention(right))
    .find((summary) => text(summary.recommendation && summary.recommendation.command));
}

function overviewState(attention, workspaces) {
  if (count(attention && attention.repair_required)) return "需要修复";
  if (count(attention && attention.needs_user)) return "等待你的处理";
  if (unavailableWorkspaceCount(workspaces)) return "状态不完整";
  if (count(attention && attention.ready)) return "有工作可推进";
  if (count(attention && attention.waiting)) return "等待外部条件";
  if (count(attention && attention.paused)) return "存在已暂停工作";
  return "全部正常";
}

function renderPrimaryAction(workspaces) {
  const summary = priorityWorkspace(workspaces);
  const guidance = $("primary-guidance");
  const command = $("primary-command");
  const button = $("primary-copy");
  const recommendation = summary && summary.recommendation;
  const nextCommand = text(recommendation && recommendation.command);
  if (!summary || !nextCommand) {
    guidance.textContent = "下一步 · 等待工作区建议";
    command.textContent = "尚无可复制的推荐命令";
    button.dataset.command = "";
    button.disabled = true;
    button.textContent = "复制命令";
    return;
  }
  guidance.textContent = `下一步 · ${text(summary.display_name) || text(summary.alias)}`;
  command.textContent = nextCommand;
  button.dataset.command = nextCommand;
  button.disabled = false;
  button.textContent = "复制命令";
}

async function copyCommand(command, button) {
  try {
    await navigator.clipboard.writeText(command);
    button.textContent = "已复制";
  } catch (_) {
    button.textContent = "请手动复制";
  }
}

function commandRow(command) {
  const row = element("div");
  row.className = "command";
  const code = element("code", command);
  const button = element("button", "复制命令");
  button.type = "button";
  button.addEventListener("click", () => copyCommand(command, button));
  row.append(code, button);
  return row;
}

function attentionLevel(summary) {
  const attention = summary.attention_counts || {};
  if (count(attention.repair_required)) return "danger";
  if (count(attention.needs_user)) return "warning";
  if (summary.health === "healthy") return "success";
  return "";
}

function readableWorkspaceCount(workspaces) {
  if (!Array.isArray(workspaces)) return 0;
  return workspaces.filter((summary) => text(summary.availability) === "available").length;
}

function renderTaskStatusCounts(counts, workspaces) {
  const root = $("task-status-counts");
  if (!root) return;
  root.replaceChildren();
  const readable = readableWorkspaceCount(workspaces);
  for (const key of TASK_STATUS_ORDER) {
    const card = element("div");
    card.className = "count task-count";
    if (key === "failed" && readable && count(counts && counts[key])) card.dataset.level = "danger";
    if (key === "in_progress" && readable && count(counts && counts[key])) card.dataset.level = "warning";
    const value = readable ? String(count(counts && counts[key])) : "—";
    card.append(element("strong", value), element("span", displayLabel(key, TASK_STATUS_LABELS)));
    root.append(card);
  }
}

function renderCounts(attention) {
  const root = $("attention-counts");
  root.replaceChildren();
  for (const [key, label] of [
    ["repair_required", "需要修复"],
    ["needs_user", "需要你处理"],
    ["ready", "可推进"],
    ["paused", "已暂停"],
    ["waiting", "等待中"],
  ]) {
    const card = element("div");
    card.className = "count";
    if (key === "repair_required" && count(attention && attention[key])) card.dataset.level = "danger";
    if (key === "needs_user" && count(attention && attention[key])) card.dataset.level = "warning";
    if (!count(attention && attention[key])) card.dataset.level = "success";
    card.append(element("strong", String(count(attention && attention[key]))), element("span", label));
    root.append(card);
  }
}

function renderWorkspaceCard(summary) {
  const card = element("article");
  card.className = "workspace-row";
  const identity = element("div");
  identity.className = "workspace-identity";
  const title = element("h3", text(summary.display_name) || text(summary.alias) || "未命名工作区");
  const meta = element("p", `别名：${text(summary.alias)} · 仓库 ${workspaceCount(summary, "repository_count")} · 开发线 ${workspaceCount(summary, "line_count")} · 任务 ${workspaceCount(summary, "task_count")} · 活跃目标 ${workspaceCount(summary, "active_objective_count")}`);
  meta.className = "workspace-meta";
  identity.append(title, meta);
  card.append(identity);

  const health = element("div");
  health.className = "workspace-signal";
  health.append(element("span", "状态"));
  health.firstChild.className = "workspace-signal-label";
  addBadge(health, displayLabel(summary.health, HEALTH_LABELS), attentionLevel(summary));
  card.append(health);

  const freshness = element("div");
  freshness.className = "workspace-signal";
  freshness.append(element("span", "新鲜度"));
  freshness.firstChild.className = "workspace-signal-label";
  addBadge(freshness, displayLabel(summary.freshness, FRESHNESS_LABELS));
  card.append(freshness);

  const tasks = element("div", workspaceCount(summary, "task_count"));
  tasks.className = "workspace-count";
  tasks.dataset.label = "任务数";
  card.append(tasks);
  const objectives = element("div", workspaceCount(summary, "objective_count"));
  objectives.className = "workspace-count";
  objectives.dataset.label = "目标数";
  card.append(objectives);

  const action = element("div");
  action.className = "workspace-action";
  const view = element("button", "查看摘要");
  view.className = "secondary";
  view.type = "button";
  view.addEventListener("click", () => loadWorkspace(text(summary.alias)));
  action.append(view);
  card.append(action);
  return card;
}

function renderOverview(payload) {
  const data = payload && payload.data;
  if (!data || !Array.isArray(data.workspaces)) throw new Error("OVERVIEW_UNAVAILABLE");
  const total = count(data.total_workspaces);
  const attention = data.attention_counts || {};
  const unavailable = unavailableWorkspaceCount(data.workspaces);
  $("overview-heading").textContent = total ? overviewState(attention, data.workspaces) : "尚未登记工作区";
  $("overview-summary").textContent = total
    ? unavailable
      ? `已登记 ${total} 个本地工作区，其中 ${unavailable} 个暂时不可读取；未知数据不会按 0 展示。`
      : `已加载 ${total} 个本地工作区；优先使用下一步命令继续已识别的工程工作。`
    : "尚未登记工作区。可运行 dyro setup、dyro join 或 dyro workspace add。";
  $("captured-at").textContent = text(payload.captured_at) ? `采样于 ${new Date(text(payload.captured_at)).toLocaleString("zh-CN")}` : "";
  renderCounts(data.attention_counts || {});
  renderTaskStatusCounts(data.task_status_counts || {}, data.workspaces);
  renderPrimaryAction(data.workspaces);
  const list = $("workspace-list");
  list.replaceChildren();
  if (!data.workspaces.length) {
    const empty = element("p", "没有可展示的工作区。页面不会自动创建或登记项目。");
    empty.className = "empty";
    list.append(empty);
    return;
  }
  for (const summary of data.workspaces) list.append(renderWorkspaceCard(summary));
}

function hasSurface(name) {
  return state.surfaces.includes(name);
}

function renderInventoryList(title, items, describe) {
  const section = element("div");
  section.className = "inventory";
  section.append(element("h3", title));
  if (!items.length) {
    section.append(element("p", "没有可展示的项目。"));
    return section;
  }
  const list = element("ul");
  for (const item of items) list.append(element("li", describe(item)));
  section.append(list);
  return section;
}

function renderInventory(data) {
  const root = element("div");
  root.className = "workspace-inventory";
  const lines = Array.isArray(data && data.lines) ? data.lines : [];
  const tasks = Array.isArray(data && data.tasks) ? data.tasks : [];
  const objectives = Array.isArray(data && data.objectives) ? data.objectives : [];
  root.append(
    renderInventoryList("开发线", lines, (line) => {
      const kind = displayLabel(line.kind, LINE_KIND_LABELS);
      const branch = text(line.branch) || "未提供";
      return `${text(line.id)} · ${kind} · ${branch}`;
    }),
    renderInventoryList("任务", tasks, (task) => {
      const title = text(task.title) || text(task.id) || "未命名任务";
      return `${title} · ${describeTask(task)}`;
    }),
    renderInventoryList("目标", objectives, (objective) => {
      const title = text(objective.title) || text(objective.id) || "未命名目标";
      const state = displayLabel(objective.operator_state, OPERATOR_STATE_LABELS);
      return `${title} · ${state}`;
    }),
  );
  return root;
}

function renderProofInspect(inspect) {
  const inspection = text(inspect && inspect.proof_inspection);
  const section = element("div");
  section.className = "proof-inspect";
  section.append(element("h3", inspection === "inspected" ? "独立检查 · 已检查" : "独立检查 · 未完成"));
  section.append(element("p", "独立检查只读投影，不是 task merge 放行。"));
  if (inspection !== "inspected") {
    section.append(element("p", "摘要保持未核验。独立检查未完成时不会把摘要标成已检查。"));
    return section;
  }
  const proofs = Array.isArray(inspect.proofs) ? inspect.proofs : [];
  if (!proofs.length) {
    section.append(element("p", "没有可展示的 Proof。"));
    return section;
  }
  const list = element("ul");
  for (const proof of proofs) {
    const kind = displayLabel(proof.kind, PROOF_KIND_LABELS);
    const status = proofStatusLabel(proof.kind, proof.status);
    const reason = text(proof.decay_reason);
    list.append(element("li", reason ? `${kind} · ${status} · ${displayLabel(reason, PROOF_DECAY_LABELS)}` : `${kind} · ${status}`));
  }
  section.append(list);
  const decayed = [];
  for (const objective of Array.isArray(inspect.objectives) ? inspect.objectives : []) {
    for (const item of objective.attention || []) {
      if (text(item.reason) === "PROOF_DECAYED") decayed.push(text(objective.id));
    }
  }
  if (decayed.length) section.append(element("p", `已投影衰减：${decayed.join("、")}`));
  return section;
}

async function loadProofInspect(alias) {
  if (!hasSurface("proofs")) {
    return renderProofInspect({ proof_inspection: "not_inspected", proofs: [], objectives: [] });
  }
  try {
    const payload = await request(`/api/v1/workspaces/${encodeURIComponent(alias)}/proofs`, `proofs:${alias}`);
    if (payload && payload.data) return renderProofInspect(payload.data);
  } catch (error) {
    if (error && error.message === "SESSION_EXPIRED") throw error;
  }
  return renderProofInspect({ proof_inspection: "not_inspected", proofs: [], objectives: [] });
}

function definition(label, value) {
  const wrapper = element("div");
  wrapper.append(element("dt", label), element("dd", value));
  return wrapper;
}

function firstWarning(payload) {
  const warnings = payload && payload.freshness && payload.freshness.warnings;
  if (!Array.isArray(warnings) || !warnings.length) return "";
  return text(warnings[0] && warnings[0].code);
}

function updateKindLabel(kind, latest) {
  if (kind === "patch" || kind === "minor" || kind === "major") {
    return displayLabel(kind, UPDATE_KIND_LABELS);
  }
  return text(latest) ? "无更高版本" : "无已缓存更新";
}

function renderSystem(payload, failed = false) {
  const panel = $("system-panel");
  const note = $("system-note");
  const update = $("system-update");
  if (!panel || !note || !update) return;
  if (!hasSurface("system")) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  if (failed || !payload) {
    note.textContent = failed
      ? "本机系统暂时不可读取。本页不探测 PATH，也不发起网络检查。"
      : "本机系统尚未读取。点刷新后只读已缓存的更新记录，不探测 PATH。";
    update.replaceChildren();
    return;
  }
  const warning = firstWarning(payload);
  const unread = warning === "UPDATE_STATE_UNAVAILABLE";
  note.textContent = unread
    ? "更新缓存不可读。本页不探测 PATH，也不发起网络检查。"
    : "本页只读已缓存的更新记录，不探测 PATH，也不发起网络检查。";
  const cached = payload.data && payload.data.update ? payload.data.update : {};
  update.replaceChildren(
    definition("检查开关", unread ? "—" : (cached.check_enabled ? "已启用" : "已关闭")),
    definition("上次检查", unread ? "—" : (text(cached.last_checked_on) || "—")),
    definition("缓存最新版", unread ? "—" : (text(cached.latest_version) || "—")),
    definition("相对当前版本", unread ? "—" : updateKindLabel(cached.kind, cached.latest_version)),
    definition("本机工具", "摘要未探测"),
  );
}

async function loadSystem() {
  if (!hasSurface("system")) {
    state.system = null;
    renderSystem(null);
    return;
  }
  try {
    const payload = await request("/api/v1/system", "system");
    if (payload) state.system = payload;
    renderSystem(state.system);
  } catch (error) {
    if (error && error.message === "SESSION_EXPIRED") throw error;
    state.system = null;
    renderSystem(null, true);
  }
}

async function loadWorkspace(alias, silent = false) {
  if (!SAFE_ID.test(alias)) return;
  try {
    const payload = await request(`/api/v1/workspaces/${encodeURIComponent(alias)}`, `workspace:${alias}`);
    if (!payload) return;
    const summary = payload.data && payload.data.workspace;
    if (!summary) throw new Error("WORKSPACE_UNAVAILABLE");
    const detail = $("workspace-detail");
    const content = $("detail-content");
    const grid = element("dl");
    grid.className = "detail-grid";
    grid.append(
      definition("别名", text(summary.alias)),
      definition("健康", displayLabel(summary.health, HEALTH_LABELS)),
      definition("可用性", displayLabel(summary.availability, AVAILABILITY_LABELS)),
      definition("仓库", workspaceCount(summary, "repository_count")),
      definition("任务总数", workspaceCount(summary, "task_count")),
      definition("开发线", workspaceCount(summary, "line_count")),
      definition("目标", workspaceCount(summary, "objective_count")),
      definition("摘要 Proof", displayLabel(summary.proof_inspection, PROOF_INSPECTION_LABELS)),
    );
    content.replaceChildren(grid);
    content.append(renderInventory(payload.data));
    const command = text(summary.recommendation && summary.recommendation.command);
    if (command) content.append(commandRow(command));
    content.append(await loadProofInspect(alias));
    detail.hidden = false;
    $("detail-heading").focus();
  } catch (error) {
    if (error && error.message === "SESSION_EXPIRED") {
      expireSession();
      return;
    }
    if (!silent) showError(error);
  }
}

function showError(error) {
  const code = text(error && error.message) || "LOCAL_READ_UNAVAILABLE";
  const message = userError(code);
  setStatus(`${message}。可重新运行 dyro console。`, true);
  const list = $("workspace-list");
  list.replaceChildren();
  const notice = element("p", `${message}。Console 未修改任何项目文件。`);
  notice.className = "error";
  list.append(notice);
}

async function refresh({ includeSystem = false } = {}) {
  try {
    const payload = await request("/api/v1/overview?limit=100", "overview");
    if (payload) {
      renderOverview(payload);
      state.partial = Boolean(payload.freshness && payload.freshness.partial);
    }
    if (includeSystem) {
      await loadSystem();
    } else {
      renderSystem(state.system);
    }
    setStatus(
      state.partial ? "本地会话已就绪；部分工作区状态未能读取，页面只读。" : "本地会话已就绪；页面只读。",
      false,
    );
  } catch (error) {
    if (error && error.message === "SESSION_EXPIRED") {
      expireSession();
      return;
    }
    showError(error);
  }
}

function scheduleRefresh() {
  window.clearTimeout(state.timer);
  if (state.bearer && !document.hidden) state.timer = window.setTimeout(async () => {
    await refresh();
    scheduleRefresh();
  }, 5000);
}

async function start() {
  $("refresh").addEventListener("click", async () => { await refresh({ includeSystem: true }); scheduleRefresh(); });
  $("primary-copy").addEventListener("click", () => {
    const button = $("primary-copy");
    const command = text(button.dataset.command);
    if (command) copyCommand(command, button);
  });
  $("detail-close").addEventListener("click", () => { $("workspace-detail").hidden = true; });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      scheduleRefresh();
      return;
    }
    refresh().finally(scheduleRefresh);
  });
  const bootstrap = consumeFragment();
  try {
    if (bootstrap) await exchange(bootstrap);
    else state.bearer = sessionStorage.getItem(TOKEN_KEY) || "";
    if (!state.bearer) throw new Error("SESSION_EXPIRED");
    const meta = await request("/api/v1/meta", "meta");
    if (meta) {
      const surfaces = meta.data && (meta.data.surfaces || meta.data.capabilities);
      state.surfaces = Array.isArray(surfaces)
        ? surfaces.filter((item) => typeof item === "string")
        : [];
      if (!state.focus) state.focus = text(meta.data && meta.data.initial_workspace);
    }
    await refresh({ includeSystem: true });
    scheduleRefresh();
  } catch (error) {
    if (error && error.message === "SESSION_EXPIRED") {
      expireSession();
      return;
    } else {
      sessionStorage.removeItem(TOKEN_KEY);
      setStatus("无法建立本地会话；请重新运行 dyro console。", true);
    }
    showError(error);
  }
}

start();
