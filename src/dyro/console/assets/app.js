const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const TOKEN_KEY = "dyro.console.bearer";
const state = { bearer: "", etags: new Map(), timer: null, focus: "" };

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
    card.append(element("strong", String(count(attention && attention[key]))), element("span", label));
    root.append(card);
  }
}

function renderWorkspaceCard(summary) {
  const card = element("article");
  card.className = "workspace-card";
  const header = element("header");
  const title = element("h3", text(summary.display_name) || text(summary.alias) || "未命名工作区");
  const badges = element("div");
  badges.className = "badges";
  addBadge(badges, text(summary.health) || "unavailable", attentionLevel(summary));
  addBadge(badges, text(summary.freshness) || "partial");
  header.append(title, badges);
  card.append(header);

  const meta = element("p", `别名：${text(summary.alias)} · Task：${count(summary.task_count)} · Active Objective：${count(summary.active_objective_count)}`);
  meta.className = "workspace-meta";
  card.append(meta);
  const statuses = summary.task_status_counts && typeof summary.task_status_counts === "object"
    ? Object.entries(summary.task_status_counts).filter(([, value]) => Number.isInteger(value) && value > 0)
    : [];
  if (statuses.length) {
    const execution = element("p", `任务状态：${statuses.map(([key, value]) => `${key} ${value}`).join(" · ")}`);
    execution.className = "workspace-meta";
    card.append(execution);
  }

  const recommendation = summary.recommendation || {};
  const reason = text(recommendation.reason) || "LOCAL_READ_UNAVAILABLE";
  const priority = element("p", `当前关注：${reason}`);
  priority.className = "priority";
  card.append(priority);
  const command = text(recommendation.command);
  if (command) card.append(commandRow(command));

  const footer = element("footer");
  const view = element("button", "查看摘要");
  view.type = "button";
  view.addEventListener("click", () => loadWorkspace(text(summary.alias)));
  footer.append(view);
  card.append(footer);
  return card;
}

function renderOverview(payload) {
  const data = payload && payload.data;
  if (!data || !Array.isArray(data.workspaces)) throw new Error("OVERVIEW_UNAVAILABLE");
  const total = count(data.total_workspaces);
  $("overview-summary").textContent = total ? `已加载 ${total} 个本地工作区；先处理有明确恢复路径的事项。` : "尚未登记工作区。可运行 dyro setup、dyro join 或 dyro workspace add。";
  $("captured-at").textContent = text(payload.captured_at) ? `采样时间：${text(payload.captured_at)}` : "";
  renderCounts(data.attention_counts || {});
  const list = $("workspace-list");
  list.replaceChildren();
  if (!data.workspaces.length) {
    const empty = element("p", "没有可展示的工作区。页面不会自动创建或登记项目。");
    empty.className = "empty";
    list.append(empty);
    return;
  }
  for (const summary of data.workspaces) list.append(renderWorkspaceCard(summary));
  if (state.focus) loadWorkspace(state.focus, true);
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
      definition("健康", text(summary.health)),
      definition("可用性", text(summary.availability)),
      definition("Task 总数", String(count(summary.task_count))),
      definition("开发线", String(count(summary.line_count))),
      definition("Objective", String(count(summary.objective_count))),
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
  setStatus(`无法读取本地状态：${code}。可重新运行 dyro console。`, true);
  const list = $("workspace-list");
  list.replaceChildren();
  const notice = element("p", `读取失败：${code}。Console 未修改任何项目文件。`);
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
