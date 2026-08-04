const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const TOKEN_KEY = "dyro.console.bearer";
const state = { bearer: "", etags: new Map(), timer: null, focus: "" };
const HEALTH_LABELS = { healthy: "健康", degraded: "需关注", unavailable: "不可用" };
const FRESHNESS_LABELS = { fresh: "新鲜", partial: "部分可用", stale: "待更新" };
const AVAILABILITY_LABELS = { available: "可用", unavailable: "不可用" };
const ERROR_LABELS = {
  LOCAL_READ_UNAVAILABLE: "本地状态暂时不可读取",
  OVERVIEW_UNAVAILABLE: "工作区概览暂时不可读取",
  WORKSPACE_UNAVAILABLE: "工作区当前不可用",
  SESSION_REJECTED: "本地会话未建立",
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

function displayLabel(value, labels) {
  const raw = text(value);
  return labels[raw] || raw || "未提供";
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

function overviewState(attention) {
  if (count(attention && attention.repair_required)) return "需要修复";
  if (count(attention && attention.needs_user)) return "等待你的处理";
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
  const meta = element("p", `别名：${text(summary.alias)} · 任务 ${count(summary.task_count)} · 活跃目标 ${count(summary.active_objective_count)}`);
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

  const tasks = element("div", String(count(summary.task_count)));
  tasks.className = "workspace-count";
  tasks.dataset.label = "任务数";
  card.append(tasks);
  const objectives = element("div", String(count(summary.objective_count)));
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
  $("overview-heading").textContent = total ? overviewState(attention) : "尚未登记工作区";
  $("overview-summary").textContent = total ? `已加载 ${total} 个本地工作区；优先使用下一步命令继续已识别的工程工作。` : "尚未登记工作区。可运行 dyro setup、dyro join 或 dyro workspace add。";
  $("captured-at").textContent = text(payload.captured_at) ? `采样于 ${new Date(text(payload.captured_at)).toLocaleString("zh-CN")}` : "";
  renderCounts(data.attention_counts || {});
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

function definition(label, value) {
  const wrapper = element("div");
  wrapper.append(element("dt", label), element("dd", value));
  return wrapper;
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
      definition("任务总数", String(count(summary.task_count))),
      definition("开发线", String(count(summary.line_count))),
      definition("目标", String(count(summary.objective_count))),
    );
    content.replaceChildren(grid);
    const command = text(summary.recommendation && summary.recommendation.command);
    if (command) content.append(commandRow(command));
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

async function refresh() {
  try {
    const payload = await request("/api/v1/overview?limit=100", "overview");
    if (payload) renderOverview(payload);
    setStatus("本地会话已就绪；页面只读。", false);
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
  $("refresh").addEventListener("click", async () => { await refresh(); scheduleRefresh(); });
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
    if (meta && !state.focus) state.focus = text(meta.data && meta.data.initial_workspace);
    await refresh();
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
