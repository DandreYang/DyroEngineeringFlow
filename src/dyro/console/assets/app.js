const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const TOKEN_KEY = "dyro.console.bearer";
const state = {
  bearer: "",
  etags: new Map(),
  timer: null,
  focus: "",
  partial: false,
  surfaces: [],
  system: null,
  detailAlias: "",
  detailTab: "family",
  eventCursor: "",
  eventAbort: null,
  eventPoll: null,
  eventItems: [],
  liveEdges: new Set(),
  sseFailed: false,
  familyParent: "",
};
const HEALTH_LABELS = { healthy: "健康", degraded: "需关注", unavailable: "不可用" };
const FRESHNESS_LABELS = { fresh: "读取完整", partial: "部分可读", stale: "待刷新" };
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
const REQUESTED_MODE_LABELS = {
  observe: "只观察",
  supervised: "监督执行",
  automatic: "自动执行",
};
const ATTENTION_KIND_LABELS = {
  repair_required: "需要修复",
  needs_user: "需要你处理",
  ready: "可推进",
  paused: "已暂停",
  waiting: "等待中",
};
const ATTENTION_KIND_ORDER = ["repair_required", "needs_user", "ready", "paused", "waiting"];
const ATTENTION_REASON_LABELS = {
  TASK_READY: "有任务可以继续做",
  TASK_REVIEW_READY: "有任务等你复核",
  DEPENDENCY_PENDING: "还在等依赖完成",
  DECISION_OPEN: "有个决定还没做",
  ANSWER_REQUIRED: "需要你回答一个问题",
  EXTERNAL_CLAIM_ACTIVE: "这条任务还被别人占着",
  CONFLICT_GROUP_ACTIVE: "和另一条任务冲突，还不能并行",
  TASK_INTEGRATION_PENDING: "做完了，还没合入",
  TASK_FAILED: "有任务失败了",
  TRIGGER_NOT_DUE: "还没到下次检查时间",
  BUDGET_EXHAUSTED: "这轮预算用完了",
  NO_PROGRESS: "连续几轮没有进展",
  CONTRACT_DRIFT: "目标和实际状态对不上",
  ACTION_UNCERTAIN: "下一步还不明确",
  TARGETS_INTEGRATED: "目标已经合入",
  OBJECTIVE_SCOPE_CONFLICT: "目标和范围冲突",
  OBJECTIVE_PAUSED: "这个目标已暂停",
  ACTIVATION_REQUIRED: "需要你确认后才能继续",
  POLICY_DISALLOWS_OPERATION: "当前策略不允许这一步",
  HOME_GUIDANCE: "可以先打开这个项目看看",
  WORKSPACE_UNAVAILABLE: "这个项目现在读不到",
};
const PROOF_INSPECTION_LABELS = { not_inspected: "尚未核验证据", inspected: "已单独检查证据" };
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
  EVENT_CURSOR_INVALID: "事件游标已失效，已从头读取",
  EVENT_STREAM_UNAVAILABLE: "实时事件流不可用，已回退轮询",
  FAMILY_NOT_FOUND: "没有可展示的一层家族",
};
const EVENT_KIND_LABELS = {
  spawn: "子线已创建",
  merge: "子线已合入父线",
  sync: "父线已同步到子线",
  task_status: "任务状态已更新",
  objective_wave: "目标波次",
  dispatch: "派发",
  board: "会审记录",
  signal: "家族信号",
  host_seed: "已写入 overlay",
  EVENT_REDACTED: "已脱敏事件",
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
  // 摘要路径只能是 not_inspected。写成「已合入」会把未核验摘要伪装成 merge 放行。
  return text(state) === "not_inspected" ? "尚未核验是否已合入" : "未提供";
}

function describeTask(task) {
  const status = displayLabel(task && task.status, TASK_STATUS_LABELS);
  const integration = taskIntegrationLabel(task && task.integration_state);
  const executor = text(task && task.executor);
  const reviewer = text(task && task.reviewer);
  const roles = [
    executor ? `执行 ${executor}` : "",
    reviewer ? `复核 ${reviewer}` : "",
  ].filter(Boolean).join(" · ");
  const blocked = Array.isArray(task && task.blocked_on) && task.blocked_on.length
    ? `阻塞于 ${task.blocked_on.map((item) => text(item)).filter(Boolean).join("、")}`
    : "";
  return [status, integration, roles, blocked].filter(Boolean).join(" · ");
}

function describeObjective(objective) {
  const title = text(objective && objective.title) || text(objective && objective.id) || "未命名目标";
  const state = displayLabel(objective && objective.operator_state, OPERATOR_STATE_LABELS);
  const mode = text(objective && objective.requested_mode)
    ? displayLabel(objective.requested_mode, REQUESTED_MODE_LABELS)
    : "";
  return [title, state, mode].filter(Boolean).join(" · ");
}

function attentionKindRank(item) {
  const index = ATTENTION_KIND_ORDER.indexOf(text(item && item.kind));
  return index === -1 ? ATTENTION_KIND_ORDER.length : index;
}

function describeAttentionItem(item, objective) {
  if (!item) return "";
  const reason = displayLabel(item.reason, ATTENTION_REASON_LABELS);
  const subject = text(item.subject_id);
  const owner = objective ? (text(objective.title) || text(objective.id)) : "";
  if (owner && subject) return `${owner}：${reason}（${subject}）`;
  if (owner) return `${owner}：${reason}`;
  if (subject) return `${reason}（${subject}）`;
  return reason;
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
  if (raw.startsWith("w/")) {
    const parts = raw.split("/").filter(Boolean);
    const alias = parts[1] || "";
    const tab = parts[2] || "family";
    state.focus = alias && SAFE_ID.test(alias) ? alias : "";
    state.detailTab = ["family", "events", "channel"].includes(tab) ? tab : "family";
    return "";
  }
  const values = new URLSearchParams(raw);
  const bootstrap = values.get("bootstrap");
  const workspace = values.get("workspace");
  state.focus = workspace && SAFE_ID.test(workspace) ? workspace : "";
  state.detailTab = "family";
  const safeRoute = state.focus ? `#w/${state.focus}/family` : "";
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${safeRoute}`);
  return bootstrap && bootstrap.length <= 256 ? bootstrap : "";
}

function setWorkspaceRoute(alias, tab) {
  const safeTab = ["family", "events", "channel"].includes(tab) ? tab : "family";
  state.detailTab = safeTab;
  const hash = alias && SAFE_ID.test(alias) ? `#w/${alias}/${safeTab}` : "";
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${hash}`);
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
  stopEventLive();
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
  if (readableWorkspaceCount(workspaces)) return "关注项未知";
  return "状态不完整";
}

function workspaceMatter(summary) {
  if (text(summary && summary.availability) !== "available") {
    return "这个项目现在读不到";
  }
  const reason = text(summary && summary.recommendation && summary.recommendation.reason);
  if (reason && reason !== "HOME_GUIDANCE") {
    return displayLabel(reason, ATTENTION_REASON_LABELS);
  }
  const attention = summary && summary.attention_counts || {};
  if (count(attention.repair_required)) return "有事项需要修复";
  if (count(attention.needs_user)) return "有事项需要你处理";
  if (count(attention.ready)) return "有工作可以继续推进";
  if (count(attention.waiting)) return "在等外部条件";
  if (count(attention.paused)) return "有工作已暂停";
  return "摘要未列出关注项";
}

function needsYouWorkspaces(workspaces) {
  if (!Array.isArray(workspaces)) return [];
  return workspaces
    .filter((summary) => {
      if (text(summary.availability) !== "available") return true;
      const attention = summary.attention_counts || {};
      return Boolean(count(attention.repair_required) || count(attention.needs_user));
    })
    .sort((left, right) => workspaceAttention(left) - workspaceAttention(right));
}

function renderNeedsYou(workspaces, total) {
  const root = $("needs-you");
  if (!root) return;
  root.replaceChildren();
  const heading = element("h3", "现在需要你");
  heading.className = "needs-you-heading";
  root.append(heading);
  if (!total) {
    root.append(element("p", "还没有登记项目。可运行 dyro setup、dyro join 或 dyro workspace add。"));
    return;
  }
  const items = needsYouWorkspaces(workspaces);
  if (!items.length) {
    const readable = readableWorkspaceCount(workspaces);
    if (!readable) {
      root.append(element("p", "项目状态还不完整，关注项未知。"));
      return;
    }
    const listed = workspaces.some((summary) => {
      if (text(summary.availability) !== "available") return false;
      const attention = summary.attention_counts || {};
      return Boolean(
        count(attention.ready) || count(attention.waiting) || count(attention.paused)
      );
    });
    root.append(element("p", listed
      ? "现在没有需要你当场处理的项目。"
      : "摘要未列出关注项。"));
    return;
  }
  for (const summary of items) {
    const row = element("article");
    row.className = "needs-you-item";
    const level = attentionLevel(summary);
    if (level) row.dataset.level = level;
    const title = element("strong", text(summary.display_name) || text(summary.alias) || "未命名项目");
    const why = element("p", workspaceMatter(summary));
    const actions = element("div");
    actions.className = "needs-you-actions";
    const command = text(summary.recommendation && summary.recommendation.command);
    if (command) {
      const copy = element("button", "复制命令");
      copy.type = "button";
      copy.addEventListener("click", () => copyCommand(command, copy));
      actions.append(copy);
    }
    const view = element("button", "打开项目");
    view.type = "button";
    view.className = "secondary";
    view.addEventListener("click", () => loadWorkspace(text(summary.alias)));
    actions.append(view);
    row.append(title, why, actions);
    root.append(row);
  }
}

function renderPrimaryAction(workspaces) {
  const summary = priorityWorkspace(workspaces);
  const guidance = $("primary-guidance");
  const why = $("primary-why");
  const command = $("primary-command");
  const button = $("primary-copy");
  const recommendation = summary && summary.recommendation;
  const nextCommand = text(recommendation && recommendation.command);
  if (!summary || !nextCommand) {
    guidance.textContent = "下一步 · 还没有可执行的建议";
    if (why) why.textContent = "等项目状态可读之后，这里会给出一条可以贴到终端的命令。";
    command.textContent = "尚无可复制的推荐命令";
    button.dataset.command = "";
    button.disabled = true;
    button.textContent = "复制命令";
    return;
  }
  const name = text(summary.display_name) || text(summary.alias);
  guidance.textContent = `下一步 · ${name}`;
  if (why) why.textContent = `${workspaceMatter(summary)}。把命令贴到终端里处理，这页不会替你执行。`;
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

  const matter = element("div");
  matter.className = "workspace-signal";
  matter.append(element("span", "当前事项"));
  matter.firstChild.className = "workspace-signal-label";
  addBadge(matter, workspaceMatter(summary), attentionLevel(summary));
  card.append(matter);

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
  const view = element("button", "打开项目");
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
      ? `${total} 个项目里有 ${unavailable} 个现在读不到。下面先列出需要你处理的事。`
      : `${total} 个本地项目。先看需要你的事，再把命令贴到终端。`
    : "还没有登记项目。可运行 dyro setup、dyro join 或 dyro workspace add。";
  $("captured-at").textContent = text(payload.captured_at) ? `读取于 ${new Date(text(payload.captured_at)).toLocaleString("zh-CN")}` : "";
  renderCounts(data.attention_counts || {});
  renderNeedsYou(data.workspaces, total);
  renderPrimaryAction(data.workspaces);
  renderTaskStatusCounts(data.task_status_counts || {}, data.workspaces);
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
  root.append(renderWorkspaceAttention(data));
  const available = text(data && data.workspace && data.workspace.availability) === "available";
  if (!available) {
    return root;
  }
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
    renderInventoryList("目标", objectives, describeObjective),
  );
  return root;
}

function renderWorkspaceAttention(data) {
  const section = element("div");
  section.className = "inventory";
  section.append(element("h3", "需要关注"));
  const available = text(data && data.workspace && data.workspace.availability) === "available";
  if (!available) {
    section.append(element("p", "工作区不可读取，关注项未知。"));
    return section;
  }
  const items = [];
  for (const objective of Array.isArray(data && data.objectives) ? data.objectives : []) {
    for (const item of Array.isArray(objective.attention) ? objective.attention : []) {
      items.push({ objective, item });
    }
  }
  items.sort((left, right) => attentionKindRank(left.item) - attentionKindRank(right.item));
  if (!items.length) {
    section.append(element("p", "摘要未列出关注项。"));
    return section;
  }
  const list = element("ul");
  for (const entry of items) list.append(element("li", describeAttentionItem(entry.item, entry.objective)));
  section.append(list);
  return section;
}

function renderProofInspect(inspect) {
  const inspection = text(inspect && inspect.proof_inspection);
  const section = element("div");
  section.className = "proof-inspect";
  section.append(element("h3", inspection === "inspected" ? "证据检查 · 已核对" : "证据检查 · 还没核对"));
  section.append(element("p", "这是只读核对，不能代替合并。"));
  if (inspection !== "inspected") {
    section.append(element("p", "上面的项目摘要还没有核验证据。核对失败时，不会把摘要标成已检查。"));
    return section;
  }
  const proofs = Array.isArray(inspect.proofs) ? inspect.proofs : [];
  if (!proofs.length) {
    section.append(element("p", "没有可展示的证据记录。"));
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
  if (decayed.length) section.append(element("p", `这些目标的证据已经过期：${decayed.join("、")}`));
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
      ? "这台电脑的更新记录暂时读不到。不会扫描本机工具，也不会联网。"
      : "还没读取这台电脑的更新记录。点刷新后只看上次记下的版本。";
    update.replaceChildren();
    return;
  }
  const warning = firstWarning(payload);
  const unread = warning === "UPDATE_STATE_UNAVAILABLE";
  note.textContent = unread
    ? "更新记录读不到。不会扫描本机工具，也不会联网。"
    : "只显示上次记下的更新。不会扫描本机工具，也不会联网。";
  const cached = payload.data && payload.data.update ? payload.data.update : {};
  update.replaceChildren(
    definition("自动检查", unread ? "—" : (cached.check_enabled ? "已打开" : "已关闭")),
    definition("上次检查", unread ? "—" : (text(cached.last_checked_on) || "—")),
    definition("记下的新版本", unread ? "—" : (text(cached.latest_version) || "—")),
    definition("和现在比", unread ? "—" : updateKindLabel(cached.kind, cached.latest_version)),
    definition("本机工具", "还未检查本机工具"),
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

function stopEventLive() {
  if (state.eventAbort) {
    state.eventAbort.abort();
    state.eventAbort = null;
  }
  if (state.eventPoll) {
    window.clearTimeout(state.eventPoll);
    state.eventPoll = null;
  }
}

function familyChildren(lines, parentId) {
  return lines
    .filter((line) => text(line.parent) === parentId && SAFE_ID.test(text(line.id)))
    .map((line) => text(line.id));
}

function familyParents(lines) {
  return lines.map((line) => text(line.id)).filter((id) => SAFE_ID.test(id));
}

function lineInProgress(tasks, lineId) {
  return tasks.some((task) => text(task.line) === lineId && text(task.status) === "in_progress");
}

function dryRunCommands(alias, parent, child) {
  return [
    `dyro --workspace ${alias} --dry-run line spawn ${parent} ${child}`,
    `dyro --workspace ${alias} --dry-run line merge ${child} --into ${parent}`,
    `dyro --workspace ${alias} --dry-run line sync ${child}`,
  ];
}

function renderFamilyTree(alias, lines, tasks) {
  const section = element("section");
  section.className = "live-pane family-pane";
  section.id = "family-pane";
  section.append(element("h3", "家族"));
  if (!hasSurface("events")) {
    section.append(element("p", "家族树尚未开放。"));
    return section;
  }
  const parents = familyParents(lines);
  if (!parents.length) {
    section.append(element("p", "这个项目还没有开发线。"));
    return section;
  }
  const selected = parents.includes(state.familyParent) ? state.familyParent : parents[0];
  state.familyParent = selected;
  if (parents.length > 1) {
    const nav = element("div");
    nav.className = "family-picker";
    nav.setAttribute("role", "tablist");
    for (const parent of parents) {
      const button = element("button", parent);
      button.type = "button";
      button.className = "secondary";
      if (parent === selected) button.setAttribute("aria-current", "true");
      button.addEventListener("click", () => {
        state.familyParent = parent;
        const tree = $("family-tree");
        if (tree) tree.replaceWith(renderFamilyGraph(alias, lines, parent, tasks));
      });
      nav.append(button);
    }
    section.append(nav);
  }
  section.append(renderFamilyGraph(alias, lines, selected, tasks));
  return section;
}

function familyBadges(id, tasks) {
  const marks = element("p");
  marks.className = "family-badges";
  if (id === "operator") {
    marks.append(element("span", "未读 0"));
    return marks;
  }
  const busy = lineInProgress(tasks, id);
  // Git cleanliness and origin binding are not inspected in P1.
  for (const label of [
    "未检查",
    "未检查",
    busy ? "进行中" : "空闲",
    "未读 0",
  ]) {
    const badge = element("span", label);
    badge.className = "family-badge";
    marks.append(badge);
  }
  return marks;
}

function edgeLabel(parent, child, live) {
  return live ? `${parent} → ${child} · 刚有合入或同步` : `${parent} → ${child}`;
}

function renderFamilyGraph(alias, lines, parent, tasks) {
  const wrap = element("div");
  wrap.id = "family-tree";
  wrap.className = "family-tree";
  const children = familyChildren(lines, parent);
  const members = [parent, ...children, "operator"];
  const list = element("ul");
  list.className = "family-nodes";
  for (const id of members) {
    const item = element("li");
    item.className = "family-node";
    item.dataset.role = id === parent ? "parent" : id === "operator" ? "operator" : "child";
    const title = element("strong", id);
    const role = element(
      "span",
      id === parent ? "父线" : id === "operator" ? "操作者" : "子线",
    );
    role.className = "family-role";
    item.append(title, role, familyBadges(id, tasks));
    if (id !== "operator" && id !== parent) {
      const live = state.liveEdges.has(`${parent}>${id}`) || state.liveEdges.has(`${id}>${parent}`);
      const edge = element("span", edgeLabel(parent, id, live));
      edge.className = live ? "family-edge live" : "family-edge";
      edge.dataset.from = parent;
      edge.dataset.to = id;
      item.append(edge);
    }
    list.append(item);
  }
  wrap.append(list);
  const actions = element("div");
  actions.className = "family-actions";
  const child = children[0] || `${parent}_new`;
  for (const command of dryRunCommands(alias, parent, child)) {
    actions.append(commandRow(command));
  }
  wrap.append(element("p", "复制区只有 dry-run。页面不会执行 spawn、合入或同步。"));
  wrap.append(actions);
  return wrap;
}

function describeEvent(event) {
  const kind = displayLabel(event && event.kind, EVENT_KIND_LABELS);
  const actor = text(event && event.actor);
  const subject = text(event && event.subject);
  const when = text(event && event.at);
  const local = when ? new Date(when).toLocaleString("zh-CN") : "";
  const who = [actor, subject].filter(Boolean).join(" → ");
  return [local, kind, who].filter(Boolean).join(" · ");
}

function applyLiveEdges(events) {
  state.liveEdges = new Set();
  for (const event of events) {
    const parent = text(event && event.facts && event.facts.parent);
    const child = text(event && event.facts && event.facts.child);
    if (text(event && event.kind) === "merge" && parent && child) {
      state.liveEdges.add(`${child}>${parent}`);
    }
    if (text(event && event.kind) === "sync" && parent && child) {
      state.liveEdges.add(`${parent}>${child}`);
    }
  }
  const tree = $("family-tree");
  if (!tree) return;
  for (const edge of tree.querySelectorAll(".family-edge")) {
    const from = text(edge.dataset.from);
    const to = text(edge.dataset.to);
    const live = state.liveEdges.has(`${from}>${to}`) || state.liveEdges.has(`${to}>${from}`);
    edge.classList.toggle("live", live);
    edge.textContent = edgeLabel(from, to, live);
  }
}

function renderEventList() {
  const list = $("event-list");
  if (!list) return;
  list.replaceChildren();
  if (!state.eventItems.length) {
    list.append(element("li", "还没有直播事件。"));
    return;
  }
  for (const event of [...state.eventItems].reverse()) {
    list.append(element("li", describeEvent(event)));
  }
}

function appendEvents(events) {
  if (!Array.isArray(events) || !events.length) return;
  const seen = new Set(state.eventItems.map((item) => text(item.id)));
  for (const event of events) {
    const id = text(event && event.id);
    if (!id || seen.has(id)) continue;
    state.eventItems.push(event);
    seen.add(id);
  }
  if (state.eventItems.length > 100) {
    state.eventItems = state.eventItems.slice(-100);
  }
  applyLiveEdges(state.eventItems);
  renderEventList();
}

function consumeSse(buffer) {
  const parts = buffer.split("\n\n");
  const rest = parts.pop() || "";
  for (const block of parts) {
    let data = "";
    let id = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("data:")) data += line.slice(5).trim();
      if (line.startsWith("id:")) id = line.slice(3).trim();
    }
    if (id && /^[A-Za-z0-9_-]+$/.test(id)) state.eventCursor = id;
    if (!data || data.startsWith(":")) continue;
    try {
      const parsed = JSON.parse(data);
      if (parsed && typeof parsed === "object") appendEvents([parsed]);
    } catch (_) {
      /* ignore a partial or redacted frame */
    }
  }
  return rest;
}

async function pullEvents(alias) {
  const after = state.eventCursor ? `?after=${encodeURIComponent(state.eventCursor)}` : "";
  const payload = await request(
    `/api/v1/workspaces/${encodeURIComponent(alias)}/events${after}`,
    `events:${alias}:${state.eventCursor || "0"}`,
  );
  if (!payload || !payload.data) return;
  appendEvents(Array.isArray(payload.data.events) ? payload.data.events : []);
  const cursor = text(payload.data.next_cursor);
  if (cursor) state.eventCursor = cursor;
}

function startEventPoll(alias) {
  window.clearTimeout(state.eventPoll);
  if (!state.bearer || document.hidden || state.detailAlias !== alias) return;
  state.eventPoll = window.setTimeout(async () => {
    try {
      await pullEvents(alias);
    } catch (error) {
      if (error && error.message === "SESSION_EXPIRED") {
        expireSession();
        return;
      }
      if (error && error.message === "EVENT_CURSOR_INVALID") {
        state.eventCursor = "";
        state.etags.delete(`events:${alias}:${state.eventCursor || "0"}`);
      }
    }
    startEventPoll(alias);
  }, 5000);
}

async function openEventStream(alias) {
  const controller = new AbortController();
  state.eventAbort = controller;
  const after = state.eventCursor ? `?after=${encodeURIComponent(state.eventCursor)}` : "";
  const response = await fetch(
    `/api/v1/workspaces/${encodeURIComponent(alias)}/events/stream${after}`,
    {
      headers: { Authorization: `Bearer ${state.bearer}` },
      cache: "no-store",
      credentials: "omit",
      signal: controller.signal,
    },
  );
  if (response.status === 401) throw new Error("SESSION_EXPIRED");
  if (response.status === 405 || !response.ok || !response.body) {
    throw new Error("EVENT_STREAM_UNAVAILABLE");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer = consumeSse(buffer + decoder.decode(value, { stream: true }));
  }
}

async function startEventLive(alias) {
  stopEventLive();
  if (!hasSurface("events") || document.hidden || !SAFE_ID.test(alias)) return;
  state.detailAlias = alias;
  if (state.sseFailed) {
    try {
      await pullEvents(alias);
    } catch (error) {
      if (error && error.message === "SESSION_EXPIRED") throw error;
    }
    startEventPoll(alias);
    return;
  }
  try {
    await openEventStream(alias);
    if (!document.hidden && state.detailAlias === alias) {
      startEventPoll(alias);
    }
  } catch (error) {
    if (error && error.message === "SESSION_EXPIRED") throw error;
    if (error && error.name === "AbortError") return;
    state.sseFailed = true;
    try {
      await pullEvents(alias);
    } catch (pullError) {
      if (pullError && pullError.message === "SESSION_EXPIRED") throw pullError;
    }
    startEventPoll(alias);
  }
}

function renderEventPane() {
  const section = element("section");
  section.className = "live-pane event-pane";
  section.id = "event-pane";
  section.append(element("h3", "事件"));
  if (!hasSurface("events")) {
    section.append(element("p", "事件流尚未开放。"));
    return section;
  }
  const list = element("ul");
  list.id = "event-list";
  list.className = "event-list";
  section.append(list);
  return section;
}

function renderChannelPane() {
  const section = element("section");
  section.className = "live-pane channel-pane";
  section.id = "channel-pane";
  section.append(element("h3", "频道"));
  section.append(element("p", "尚未开放"));
  section.append(element("p", "产物尚未开放"));
  return section;
}

function renderLivePanes(alias, data) {
  const root = element("div");
  root.className = "live-panes";
  root.id = "live-panes";
  const tabs = element("div");
  tabs.className = "live-tabs";
  tabs.setAttribute("role", "tablist");
  for (const [id, label] of [["family", "家族"], ["events", "事件"], ["channel", "频道"]]) {
    const button = element("button", label);
    button.type = "button";
    button.dataset.tab = id;
    if (state.detailTab === id) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => {
      state.detailTab = id;
      setWorkspaceRoute(alias, id);
      root.dataset.tab = id;
      for (const item of tabs.querySelectorAll("button")) {
        if (text(item.dataset.tab) === id) item.setAttribute("aria-current", "true");
        else item.removeAttribute("aria-current");
      }
    });
    tabs.append(button);
  }
  root.dataset.tab = state.detailTab;
  root.append(tabs);
  const lines = Array.isArray(data && data.lines) ? data.lines : [];
  const tasks = Array.isArray(data && data.tasks) ? data.tasks : [];
  root.append(renderFamilyTree(alias, lines, tasks), renderEventPane(), renderChannelPane());
  return root;
}

function resetEventState() {
  stopEventLive();
  state.eventCursor = "";
  state.eventItems = [];
  state.liveEdges = new Set();
  state.sseFailed = false;
}

async function loadWorkspace(alias, silent = false) {
  if (!SAFE_ID.test(alias)) return;
  if (state.detailAlias && state.detailAlias !== alias) {
    resetEventState();
  }
  try {
    const payload = await request(`/api/v1/workspaces/${encodeURIComponent(alias)}`, `workspace:${alias}`);
    if (!payload) return;
    const summary = payload.data && payload.data.workspace;
    if (!summary) throw new Error("WORKSPACE_UNAVAILABLE");
    state.focus = alias;
    state.detailAlias = alias;
    setWorkspaceRoute(alias, state.detailTab || "family");
    const detail = $("workspace-detail");
    const content = $("detail-content");
    const grid = element("dl");
    grid.className = "detail-grid";
    grid.append(
      definition("别名", text(summary.alias)),
      definition("健康", displayLabel(summary.health, HEALTH_LABELS)),
      definition("读取情况", displayLabel(summary.freshness, FRESHNESS_LABELS)),
      definition("可用性", displayLabel(summary.availability, AVAILABILITY_LABELS)),
      definition("仓库", workspaceCount(summary, "repository_count")),
      definition("任务总数", workspaceCount(summary, "task_count")),
      definition("开发线", workspaceCount(summary, "line_count")),
      definition("目标", workspaceCount(summary, "objective_count")),
      definition("证据核验", displayLabel(summary.proof_inspection, PROOF_INSPECTION_LABELS)),
    );
    content.replaceChildren(grid);
    content.append(renderInventory(payload.data));
    const command = text(summary.recommendation && summary.recommendation.command);
    if (command) content.append(commandRow(command));
    content.append(await loadProofInspect(alias));
    content.append(renderLivePanes(alias, payload.data));
    renderEventList();
    detail.hidden = false;
    $("detail-heading").focus();
    await startEventLive(alias);
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
      state.partial ? "已连上本地页面；有些项目还读不到，这页不会改你的文件。" : "已连上本地页面；这页只看状态，不会改你的文件。",
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
  $("detail-close").addEventListener("click", () => {
    resetEventState();
    state.detailAlias = "";
    $("workspace-detail").hidden = true;
    setWorkspaceRoute("", "family");
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      scheduleRefresh();
      stopEventLive();
      return;
    }
    refresh().finally(scheduleRefresh);
    if (state.detailAlias) {
      startEventLive(state.detailAlias).catch(() => {});
    }
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
    if (state.focus) await loadWorkspace(state.focus, true);
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
