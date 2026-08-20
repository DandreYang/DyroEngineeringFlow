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
  channelItems: [],
  channelCursor: "",
  channelMembers: [],
  channelView: "list",
  channelKind: "",
  channelFrom: "",
  channelUnacked: false,
  artifactItems: [],
  artifactBlobs: new Map(),
  operatorTwin: null,
  twinTasks: [],
  twinAfterSeq: 0,
  twinOverlayComplete: false,
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
const MILESTONE_LABELS = {
  incomplete: "未完成",
  complete: "已完成",
  repair_required: "需要修复",
};
const DISPATCH_STATE_LABELS = {
  running: "在跑",
  idle: "已停",
  unknown: "状态未知",
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
  FAMILY_POST_FORBIDDEN: "人类模块不能发送这种家族信号",
  FAMILY_POST_INVALID: "家族频道请求无效",
  CHANNEL_CURSOR_INVALID: "频道游标已失效，已从头读取",
  CHANNEL_BODY_INVALID: "家族信号正文不接受",
  CHANNEL_LOG_INCONSISTENT: "家族频道日志不一致",
  ARTIFACT_NOT_FOUND: "产物不存在",
  ARTIFACT_UNAVAILABLE: "产物不可用",
  ARTIFACT_TOO_LARGE: "产物超出大小上限",
  ARTIFACT_PATH_INVALID: "产物路径无效",
};
const ARTIFACT_TYPE_LABELS = {
  review: "复核",
  image: "图像",
  chart: "图表",
  video: "视频",
};
const REVIEW_CONCLUSION_LABELS = {
  pass: "通过",
  fail: "未通过",
  inconclusive: "无法判定",
};
const SVG_NS = "http:" + "//www.w3.org/2000/svg";
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
const CHANNEL_KIND_LABELS = {
  contract: "约定",
  blocked: "阻塞",
  shipped: "声称已交付",
  ask_sync: "请求同步",
  decision: "决定",
  artifact: "产物",
  retract: "撤回",
};

const UPDATE_KIND_LABELS = {
  none: "无已缓存更新",
  patch: "有补丁更新",
  minor: "有次版本更新",
  major: "有主版本更新",
};

const $ = (id) => (typeof document !== "undefined" && document.getElementById
  ? document.getElementById(id)
  : null);

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

async function requestWrite(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${state.bearer}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    cache: "no-store",
    credentials: "omit",
  });
  const body = await response.json().catch(() => null);
  if (response.status === 401) throw new Error("SESSION_EXPIRED");
  if (!response.ok || !body) {
    const code = text(body && body.error && body.error.code) || "LOCAL_READ_UNAVAILABLE";
    throw new Error(code);
  }
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

function copyableOpenCommand(command) {
  const value = text(command);
  const dry = "--dry" + "-run";
  const yes = "--" + "yes";
  const push = "--" + "push";
  if (
    !value
    || !value.includes(dry)
    || !value.includes("line inbox")
    || value.includes(yes)
    || value.includes(push)
  ) {
    return "";
  }
  return value;
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
    const empty = element("p", "没有可展示的工作区。运行 dyro setup、dyro join 或 dyro workspace add。");
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
    section.append(element("p", title === "开发线"
      ? "没有开发线。回终端跑 dyro setup 或 line spawn。"
      : title === "任务"
        ? "没有任务。回终端建 Task 后再打开这一页。"
        : "没有目标。回终端建 Objective 后再看计划。"));
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

function emptyTwin() {
  return {
    plan: [],
    phases: TASK_STATUS_ORDER.map((status) => ({ status, tasks: [] })),
    running: [],
    latest_ledger: { present: false, at: "", task_id: "", phase: "", facts: {} },
    projected_seq: 0,
    overlay_complete: false,
  };
}

function copyTwin(twin) {
  const source = twin && typeof twin === "object" ? twin : emptyTwin();
  return {
    plan: Array.isArray(source.plan) ? source.plan.map((row) => ({
      ...row,
      task_ids: Array.isArray(row && row.task_ids) ? row.task_ids.slice() : [],
    })) : [],
    phases: Array.isArray(source.phases)
      ? source.phases.map((column) => ({
        status: text(column && column.status),
        tasks: Array.isArray(column && column.tasks)
          ? column.tasks.map((task) => ({ ...task }))
          : [],
      }))
      : emptyTwin().phases,
    running: Array.isArray(source.running) ? source.running.map((row) => ({ ...row })) : [],
    latest_ledger: source.latest_ledger && typeof source.latest_ledger === "object"
      ? { ...source.latest_ledger, facts: { ...(source.latest_ledger.facts || {}) } }
      : emptyTwin().latest_ledger,
    projected_seq: Number.isInteger(source.projected_seq) && source.projected_seq >= 0
      ? source.projected_seq
      : 0,
    overlay_complete: source.overlay_complete === true,
  };
}

function twinFromData(data) {
  const twin = data && data.operator_twin;
  if (!twin || typeof twin !== "object") return emptyTwin();
  return copyTwin(twin);
}

function findTwinTask(taskId) {
  return state.twinTasks.find((task) => text(task && task.id) === taskId) || null;
}

function knownTwinTaskId(value) {
  const id = text(value);
  if (!id) return "";
  if (findTwinTask(id)) return id;
  const twin = state.operatorTwin;
  if (!twin) return "";
  for (const column of Array.isArray(twin.phases) ? twin.phases : []) {
    for (const task of Array.isArray(column && column.tasks) ? column.tasks : []) {
      if (text(task && task.id) === id) return id;
    }
  }
  for (const row of Array.isArray(twin.running) ? twin.running : []) {
    if (text(row && row.id) === id) return id;
  }
  return "";
}

function eventKnownTaskId(event) {
  return (
    knownTwinTaskId(event && event.subject)
    || knownTwinTaskId(event && event.facts && event.facts.task_id)
    || knownTwinTaskId(event && event.facts && event.facts.task)
  );
}

function emptyRunningRow(card) {
  return {
    id: text(card && card.id),
    title: text(card && card.title),
    line: text(card && card.line),
    executor: text(card && card.executor),
    dispatch_present: false,
    dispatch_id: "",
    dispatch_at: "",
    dispatch_state: "unknown",
    dispatch_facts: {},
    board_landed: false,
  };
}

function moveTwinTask(taskId, nextStatus) {
  const twin = state.operatorTwin;
  if (!twin || !taskId || !TASK_STATUS_ORDER.includes(nextStatus)) return false;
  if (!Array.isArray(twin.phases)) twin.phases = emptyTwin().phases;
  let card = null;
  for (const column of twin.phases) {
    const tasks = Array.isArray(column.tasks) ? column.tasks : [];
    column.tasks = tasks;
    const index = tasks.findIndex((task) => text(task && task.id) === taskId);
    if (index >= 0) {
      card = tasks[index];
      tasks.splice(index, 1);
      break;
    }
  }
  const known = findTwinTask(taskId);
  if (!card && !known) return false;
  if (!card) {
    card = {
      id: text(known.id),
      title: text(known.title),
      line: text(known.line),
      executor: text(known.executor),
      status: nextStatus,
    };
  } else {
    card.status = nextStatus;
  }
  if (known) known.status = nextStatus;
  const dest = twin.phases.find((column) => text(column && column.status) === nextStatus);
  if (!dest) return false;
  dest.tasks = Array.isArray(dest.tasks) ? dest.tasks : [];
  if (!dest.tasks.some((task) => text(task && task.id) === taskId)) dest.tasks.push(card);
  dest.tasks.sort((left, right) => text(left && left.id).localeCompare(text(right && right.id)));
  twin.running = Array.isArray(twin.running) ? twin.running : [];
  const runIndex = twin.running.findIndex((row) => text(row && row.id) === taskId);
  if (nextStatus === "in_progress") {
    if (runIndex < 0) twin.running.push(emptyRunningRow(card));
  } else if (runIndex >= 0) {
    twin.running.splice(runIndex, 1);
  }
  return true;
}

function showTwinTask(taskId) {
  const panel = $("twin-task-summary");
  if (!panel) return;
  const task = findTwinTask(taskId);
  panel.replaceChildren();
  if (!task) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  panel.append(element("h4", "任务"));
  const title = text(task.title) || text(task.id) || "未命名任务";
  panel.append(element("p", `${title} · ${describeTask(task)}`));
  panel.append(element("p", "点开的是已有任务摘要。这页没有任务工作室。"));
}

function renderTwinPlan() {
  const section = element("section");
  section.className = "twin-plan";
  section.append(element("h4", "计划"));
  const plan = state.operatorTwin && Array.isArray(state.operatorTwin.plan) ? state.operatorTwin.plan : [];
  if (!plan.length) {
    section.append(element("p", "没有目标。先在终端建 Objective，再回到这页看计划。"));
    return section;
  }
  const lanes = element("div");
  lanes.className = "twin-lanes";
  for (const row of plan) {
    const lane = element("article");
    lane.className = "twin-lane";
    if (row && row.wave_present) lane.classList.add("has-wave");
    const head = element("header");
    head.className = "twin-lane-head";
    const title = element("strong", text(row && row.title) || text(row && row.id) || "未命名目标");
    const milestone = element("span", `里程碑 · ${displayLabel(row && row.milestone, MILESTONE_LABELS)}`);
    milestone.className = "twin-milestone";
    if (text(row && row.milestone) === "repair_required") milestone.dataset.level = "danger";
    if (text(row && row.milestone) === "complete") milestone.dataset.level = "ready";
    head.append(title, milestone);
    lane.append(head);
    if (row && row.wave_present) {
      const mode = text(row.wave_mode);
      lane.append(element("p", mode ? `波次 ${mode} · ${count(row.wave_count)} 项` : `波次 · ${count(row.wave_count)} 项`));
    } else {
      lane.append(element("p", "未见波次。计划仍按已有目标摊开，不另造 backlog。"));
    }
    const cells = element("div");
    cells.className = "twin-cells";
    const ids = Array.isArray(row && row.task_ids) ? row.task_ids : [];
    if (!ids.length) {
      cells.append(element("span", "这一波还没有任务格"));
    } else {
      for (const id of ids) {
        const cell = element("button", text(id));
        cell.type = "button";
        cell.className = "twin-cell";
        cell.addEventListener("click", () => showTwinTask(text(id)));
        cells.append(cell);
      }
    }
    lane.append(cells);
    lanes.append(lane);
  }
  section.append(lanes);
  return section;
}

function renderTwinPhases() {
  const section = element("section");
  section.className = "twin-phases";
  section.append(element("h4", "阶段"));
  const columns = element("div");
  columns.className = "twin-phase-grid";
  const phases = state.operatorTwin && Array.isArray(state.operatorTwin.phases)
    ? state.operatorTwin.phases
    : emptyTwin().phases;
  for (const status of TASK_STATUS_ORDER) {
    const column = phases.find((item) => text(item && item.status) === status) || { status, tasks: [] };
    const pane = element("div");
    pane.className = "twin-phase";
    pane.append(element("h5", displayLabel(status, TASK_STATUS_LABELS)));
    const tasks = Array.isArray(column.tasks) ? column.tasks : [];
    if (!tasks.length) {
      pane.classList.add("is-empty");
      pane.append(element("p", "这一列没有任务"));
    } else {
      for (const task of tasks) {
        const button = element("button", text(task.title) || text(task.id) || "未命名任务");
        button.type = "button";
        button.className = "twin-task";
        button.addEventListener("click", () => showTwinTask(text(task.id)));
        pane.append(button);
      }
    }
    columns.append(pane);
  }
  section.append(columns);
  return section;
}

function describeDispatchFacts(facts) {
  if (!facts || typeof facts !== "object") return "";
  const parts = [];
  for (const key of Object.keys(facts).sort()) {
    const value = facts[key];
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      parts.push(`${key} ${value}`);
    }
  }
  return parts.join(" · ");
}

function renderTwinRunning() {
  const section = element("section");
  section.className = "twin-running";
  section.append(element("h4", "谁在跑"));
  const running = state.operatorTwin && Array.isArray(state.operatorTwin.running) ? state.operatorTwin.running : [];
  if (!running.length) {
    section.append(element("p", "没有进行中的任务。要派人，回终端跑 dispatch。"));
  } else {
    const list = element("ul");
    list.className = "twin-running-list";
    for (const row of running) {
      const item = element("li");
      const title = element("button", text(row.title) || text(row.id) || "未命名任务");
      title.type = "button";
      title.className = "twin-task";
      title.addEventListener("click", () => showTwinTask(text(row.id)));
      const executor = text(row.executor) ? `执行 ${text(row.executor)}` : "未见执行者";
      const dispatch = row.dispatch_present
        ? displayLabel(row.dispatch_state, DISPATCH_STATE_LABELS)
        : "未见派发";
      const board = row.board_landed ? "会审已落下" : "未见会审记录";
      const facts = describeDispatchFacts(row.dispatch_facts);
      item.append(
        title,
        element("p", [executor, dispatch, board].join(" · ")),
      );
      if (facts) item.append(element("p", facts));
      list.append(item);
    }
    section.append(list);
  }
  const ledger = state.operatorTwin && state.operatorTwin.latest_ledger
    ? state.operatorTwin.latest_ledger
    : emptyTwin().latest_ledger;
  const line = element("p");
  line.className = "twin-ledger";
  if (!ledger.present) {
    line.textContent = "未见账本行。账本仍是交付审计，这页只读最近一行。";
  } else {
    const bits = ["最近账本行"];
    if (text(ledger.at)) bits.push(text(ledger.at));
    if (text(ledger.task_id)) bits.push(text(ledger.task_id));
    if (text(ledger.phase)) bits.push(text(ledger.phase));
    const facts = describeDispatchFacts(ledger.facts);
    if (facts) bits.push(facts);
    line.textContent = bits.length > 1 ? bits.join(" · ") : "最近账本行已脱敏";
  }
  section.append(line);
  return section;
}

function buildOperatorTwin() {
  const section = element("section");
  section.className = "operator-twin";
  section.id = "operator-twin";
  section.append(element("h3", "这一家现在怎样"));
  section.append(element("p", "计划、里程碑、阶段和谁在跑。页面不另造 backlog，也不改任务。"));
  section.append(renderTwinPlan(), renderTwinPhases(), renderTwinRunning());
  const summary = element("div");
  summary.id = "twin-task-summary";
  summary.className = "twin-task-summary";
  summary.hidden = true;
  section.append(summary);
  return section;
}

function renderOperatorTwin(data) {
  state.operatorTwin = twinFromData(data);
  state.twinTasks = Array.isArray(data && data.tasks) ? data.tasks : [];
  state.twinAfterSeq = state.operatorTwin.projected_seq;
  state.twinOverlayComplete = state.operatorTwin.overlay_complete === true;
  return buildOperatorTwin();
}

function applyLiveTwinEvents(twin, events, afterSeq, overlayComplete) {
  if (!twin || overlayComplete !== true || !Array.isArray(events) || !events.length) return false;
  const floor = Number.isInteger(afterSeq) && afterSeq >= 0 ? afterSeq : 0;
  const planById = new Map((twin.plan || []).map((row) => [text(row && row.id), row]));
  let changed = false;
  let maxSeq = floor;
  for (const event of events) {
    const seq = event && event.seq;
    if (!Number.isInteger(seq) || seq <= floor) continue;
    const kind = text(event && event.kind);
    const subject = text(event && event.subject);
    const actor = text(event && event.actor);
    if (kind === "objective_wave") {
      const row = planById.get(subject) || planById.get(actor);
      if (!row) continue;
      row.wave_present = true;
      row.wave_id = text(event.id);
      row.wave_at = text(event.at);
      row.wave_mode = text(event.facts && event.facts.mode);
      const value = event.facts && event.facts.count;
      if (Number.isInteger(value) && value >= 0) row.wave_count = value;
      changed = true;
    }
    if (kind === "task_status") {
      const taskId = eventKnownTaskId(event);
      const nextStatus = text(event && event.facts && event.facts.to_status);
      if (moveTwinTask(taskId, nextStatus)) changed = true;
    }
    if (kind === "dispatch") {
      const row = (twin.running || []).find((item) => text(item && item.id) === eventKnownTaskId(event));
      if (!row) continue;
      row.dispatch_present = true;
      row.dispatch_id = text(event.id);
      row.dispatch_at = text(event.at);
      const phase = text(event.facts && event.facts.phase);
      const status = text(event.facts && event.facts.status);
      row.dispatch_state = phase === "start" ? "running" : phase === "end" && status === "idle" ? "idle" : "unknown";
      row.dispatch_facts = event.facts && typeof event.facts === "object" ? event.facts : {};
      changed = true;
    }
    if (kind === "board") {
      const row = (twin.running || []).find((item) => text(item && item.id) === eventKnownTaskId(event));
      if (!row) continue;
      row.board_landed = true;
      changed = true;
    }
    if (seq > maxSeq) maxSeq = seq;
  }
  if (changed) twin.projected_seq = maxSeq;
  return changed;
}

function renderedTwinText(twin) {
  const bits = [];
  for (const row of twin && Array.isArray(twin.running) ? twin.running : []) {
    bits.push(row && row.board_landed ? "会审已落下" : "未见会审记录");
  }
  return bits.join("\n");
}

function mergeTwinFromEvents(events) {
  const twin = state.operatorTwin;
  if (!applyLiveTwinEvents(twin, events, state.twinAfterSeq, state.twinOverlayComplete)) return;
  state.twinAfterSeq = twin.projected_seq;
  const current = $("operator-twin");
  if (current) current.replaceWith(buildOperatorTwin());
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
    section.append(element("p", "家族尚未开放。运行 dyro console 后打开这一页。"));
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
        resetChannelState();
        resetArtifactState();
        loadChannel(alias, parent).catch((error) => {
          if (error && error.message === "SESSION_EXPIRED") expireSession();
        });
        loadArtifacts(alias, parent).catch((error) => {
          if (error && error.message === "SESSION_EXPIRED") expireSession();
        });
        refreshFamilyUnread(alias, parent).catch(() => {});
      });
      nav.append(button);
    }
    section.append(nav);
  }
  section.append(renderFamilyGraph(alias, lines, selected, tasks));
  return section;
}

function familyBadges(id, tasks, unread = 0) {
  const marks = element("p");
  marks.className = "family-badges";
  const unreadBadge = element("span", `未读 ${count(unread)}`);
  unreadBadge.className = "family-badge family-unread";
  if (id === "operator") {
    marks.append(unreadBadge);
    return marks;
  }
  const busy = lineInProgress(tasks, id);
  // Git cleanliness and origin binding are not inspected. Unread is overlay-only.
  for (const label of ["未检查", "未检查", busy ? "进行中" : "空闲"]) {
    const badge = element("span", label);
    badge.className = "family-badge";
    marks.append(badge);
  }
  marks.append(unreadBadge);
  return marks;
}

function edgeLabel(parent, child, live) {
  return live ? `${parent} → ${child} · 刚有合入或同步` : `${parent} → ${child}`;
}

function renderFamilyJack(id, role, tasks) {
  const jack = element("div");
  jack.className = role === "parent" ? "family-jack is-focus" : "family-jack";
  jack.dataset.member = id;
  jack.dataset.role = role;
  const title = element("strong", id);
  const label = element(
    "span",
    role === "parent" ? "父线" : role === "operator" ? "操作者" : "子线",
  );
  label.className = "family-role";
  jack.append(title, label, familyBadges(id, tasks));
  return jack;
}

function renderFamilyGraph(alias, lines, parent, tasks) {
  const wrap = element("div");
  wrap.id = "family-tree";
  wrap.className = "family-tree family-bay";
  const children = familyChildren(lines, parent);
  const stage = element("div");
  stage.className = "family-stage";
  stage.append(renderFamilyJack(parent, "parent", tasks));
  const outbound = element("div");
  outbound.className = "family-outbound";
  if (!children.length) {
    outbound.append(element("p", "还没有子线。开子线：把下面的 dry-run 贴到终端。"));
  }
  for (const id of children) {
    const run = element("div");
    run.className = "family-run";
    const live = state.liveEdges.has(`${parent}>${id}`) || state.liveEdges.has(`${id}>${parent}`);
    const thread = element("span", live ? "刚有合入或同步" : "一层亲属");
    thread.className = live ? "family-edge live" : "family-edge";
    thread.dataset.from = parent;
    thread.dataset.to = id;
    run.append(thread, renderFamilyJack(id, "child", tasks));
    outbound.append(run);
  }
  stage.append(outbound, renderFamilyJack("operator", "operator", tasks));
  wrap.append(stage);
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
    list.append(element("li", "还没有直播事件。在终端跑 line spawn、merge 或 sync 后回到这一页。"));
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
  mergeTwinFromEvents(events);
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
    section.append(renderArtifactRail(state.detailAlias, "event-artifact-rail"));
    return section;
  }
  const list = element("ul");
  list.id = "event-list";
  list.className = "event-list";
  section.append(list);
  section.append(renderArtifactRail(state.detailAlias, "event-artifact-rail"));
  return section;
}

function resetChannelState() {
  state.channelItems = [];
  state.channelCursor = "";
  state.channelMembers = [];
}

function resetArtifactState() {
  for (const url of state.artifactBlobs.values()) {
    if (typeof url === "string" && url.startsWith("blob:")) URL.revokeObjectURL(url);
  }
  state.artifactBlobs = new Map();
  state.artifactItems = [];
}

function channelRowKey(message) {
  return `${text(message && message.family)}\0${text(message && message.id)}`;
}

function artifactRowKey(family, id) {
  return `${text(family)}\0${text(id)}`;
}

function channelFilterText() {
  const parts = [];
  if (state.channelUnacked) parts.push("unacked");
  if (state.channelKind) parts.push(`kind:${state.channelKind}`);
  if (state.channelFrom) parts.push(`from:${state.channelFrom}`);
  return parts.join(",");
}

function describeChannelMessage(message) {
  const kind = text(message && message.kind);
  const label = displayLabel(kind, CHANNEL_KIND_LABELS);
  const sender = text(message && message.from) || "未提供";
  const recipient = text(message && message.to);
  const who = recipient ? `${sender} → ${recipient}` : `${sender} → 广播`;
  const when = text(message && message.at);
  const local = when ? new Date(when).toLocaleString("zh-CN") : "";
  const body = kind === "artifact"
    ? text(message && message.artifact_id) || text(message && message.id)
    : text(message && message.body);
  const flags = [
    message && message.retracted ? "已撤回" : "",
    message && message.acked ? "已读" : "未读",
  ].filter(Boolean).join(" · ");
  return [local, label, who, flags, body].filter(Boolean).join(" · ");
}

function appendChannelMessages(messages) {
  if (!Array.isArray(messages) || !messages.length) return;
  const seen = new Set(state.channelItems.map(channelRowKey));
  for (const message of messages) {
    const key = channelRowKey(message);
    if (!text(message && message.id) || seen.has(key)) continue;
    state.channelItems.push(message);
    seen.add(key);
  }
}

function visibleChannelMessages() {
  const items = [...state.channelItems];
  if (state.channelView === "list") {
    items.sort((left, right) => Number(Boolean(left.acked)) - Number(Boolean(right.acked)) || left.seq - right.seq);
  } else {
    items.sort((left, right) => left.seq - right.seq);
  }
  return items;
}

function renderChannelMessages() {
  const list = $("channel-list");
  if (!list) return;
  list.replaceChildren();
  const items = visibleChannelMessages();
  if (!items.length) {
    list.append(element("li", "还没有家族信号。"));
    return;
  }
  const alias = state.detailAlias;
  const parent = state.familyParent;
  for (const message of items) {
    const item = element("li");
    item.className = "channel-item";
    if (message.retracted) item.classList.add("retracted");
    if (!message.acked) item.classList.add("unacked");
    item.append(element("p", describeChannelMessage(message)));
    if (text(message.kind) === "artifact") {
      item.append(renderArtifactAttachment(alias, parent, message));
    }
    if (!message.acked && SAFE_ID.test(alias) && SAFE_ID.test(parent)) {
      const ack = element("button", "标为已读");
      ack.type = "button";
      ack.className = "secondary";
      ack.addEventListener("click", () => {
        postHumanChannel(alias, parent, { kind: "ack", ack_id: text(message.id) }).catch((error) => {
          if (error && error.message === "SESSION_EXPIRED") expireSession();
        });
      });
      item.append(ack);
    }
    const sender = text(message.from);
    const retractFrom = sender && sender !== "operator" ? sender : parent;
    if (
      alias &&
      retractFrom &&
      SAFE_ID.test(alias) &&
      SAFE_ID.test(retractFrom) &&
      text(message.kind) !== "retract" &&
      text(message.id)
    ) {
      item.append(commandRow(
        `dyro --workspace ${alias} --dry-run line post ${retractFrom} --kind retract --body ${text(message.id)}`,
      ));
    }
    list.append(item);
  }
}

function fillSelect(node, entries, selected) {
  if (!node) return;
  node.replaceChildren();
  for (const [value, label] of entries) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    if (value === selected) option.selected = true;
    node.append(option);
  }
}

function renderChannelFilters(members) {
  const bar = element("div");
  bar.className = "channel-toolbar";
  const views = element("div");
  views.className = "channel-views";
  views.setAttribute("role", "tablist");
  for (const [id, label] of [["list", "列表"], ["timeline", "时间线"]]) {
    const button = element("button", label);
    button.type = "button";
    button.className = "secondary";
    if (state.channelView === id) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => {
      state.channelView = id;
      for (const item of views.querySelectorAll("button")) {
        if (item.textContent === label) item.setAttribute("aria-current", "true");
        else item.removeAttribute("aria-current");
      }
      renderChannelMessages();
    });
    views.append(button);
  }
  const kind = element("select");
  kind.id = "channel-filter-kind";
  fillSelect(
    kind,
    [["", "全部 kind"], ...Object.entries(CHANNEL_KIND_LABELS)],
    state.channelKind,
  );
  kind.addEventListener("change", () => {
    state.channelKind = text(kind.value);
    reloadOpenChannel();
  });
  const from = element("select");
  from.id = "channel-filter-from";
  fillSelect(
    from,
    [["", "全部发送者"], ...members.filter((id) => id).map((id) => [id, id])],
    state.channelFrom,
  );
  from.addEventListener("change", () => {
    state.channelFrom = text(from.value);
    reloadOpenChannel();
  });
  const unacked = element("label");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = state.channelUnacked;
  box.addEventListener("change", () => {
    state.channelUnacked = Boolean(box.checked);
    reloadOpenChannel();
  });
  unacked.append(box, document.createTextNode("未读"));
  bar.append(views, kind, from, unacked);
  return bar;
}

function renderChannelCompose(alias, parent, members) {
  const form = element("div");
  form.className = "channel-compose";
  form.append(element("p", "以 operator 身份发送。页面不能假扮开发线。"));
  const kind = element("select");
  kind.id = "channel-post-kind";
  fillSelect(kind, [["decision", "决定"], ["contract", "约定"]], "decision");
  const to = element("select");
  to.id = "channel-post-to";
  fillSelect(
    to,
    [["", "全家族"], ...members.filter((id) => id && id !== "operator").map((id) => [id, id])],
    "",
  );
  const body = element("textarea");
  body.id = "channel-post-body";
  body.rows = 3;
  body.maxLength = 2048;
  const send = element("button", "发送决定");
  send.type = "button";
  kind.addEventListener("change", () => {
    send.textContent = kind.value === "contract" ? "发送约定" : "发送决定";
  });
  send.addEventListener("click", () => {
    postHumanChannel(alias, parent, {
      kind: text(kind.value),
      to: text(to.value),
      body: text(body.value),
    }).then(() => {
      body.value = "";
    }).catch((error) => {
      if (error && error.message === "SESSION_EXPIRED") expireSession();
    });
  });
  form.append(kind, to, body, send);
  form.append(element("p", "页面不能 merge、push 或改任务状态。撤回只能复制 dry-run CLI。"));
  return form;
}

function renderChannelPane(alias) {
  const section = element("section");
  section.className = "live-pane channel-pane";
  section.id = "channel-pane";
  section.append(element("h3", "决定"));
  if (!hasSurface("families")) {
    section.append(element("p", "决定尚未开放。运行 dyro console 后打开这一页。"));
    section.append(renderArtifactRail(alias, "channel-artifact-rail"));
    return section;
  }
  section.append(element("p", "以 operator 留下决定或约定。这不是交付门。"));
  const parent = state.familyParent;
  const members = state.channelMembers.length ? state.channelMembers : [];
  section.append(renderChannelFilters(members));
  const list = element("ul");
  list.id = "channel-list";
  list.className = "channel-list";
  section.append(list);
  const more = element("button", "加载更多");
  more.type = "button";
  more.id = "channel-more";
  more.className = "secondary";
  more.hidden = !state.channelCursor;
  more.addEventListener("click", () => {
    if (parent) {
      loadChannel(alias, parent).catch((error) => {
        if (error && error.message === "SESSION_EXPIRED") expireSession();
      });
    }
  });
  section.append(more);
  if (SAFE_ID.test(alias) && SAFE_ID.test(parent)) {
    section.append(renderChannelCompose(alias, parent, members));
  }
  section.append(renderArtifactRail(alias, "channel-artifact-rail"));
  return section;
}

function renderArtifactRail(alias, railId) {
  const section = element("div");
  section.className = "artifact-rail";
  section.id = railId;
  section.append(element("h4", "产物"));
  if (!hasSurface("artifacts")) {
    section.append(element("p", "产物尚未开放"));
    return section;
  }
  const list = element("div");
  list.className = "artifact-rail-list";
  section.append(list);
  fillArtifactRail(list, alias);
  return section;
}

function fillArtifactRail(list, alias) {
  if (!list) return;
  list.replaceChildren();
  if (!hasSurface("artifacts")) {
    list.append(element("p", "产物尚未开放"));
    return;
  }
  if (!state.artifactItems.length) {
    list.append(element("p", "这个家族还没有 overlay 产物。"));
    return;
  }
  for (const artifact of state.artifactItems) {
    list.append(renderArtifactView(alias, state.familyParent, artifact));
  }
}

function renderArtifactRails(alias) {
  for (const railId of ["channel-artifact-rail", "event-artifact-rail"]) {
    const rail = $(railId);
    if (!rail) continue;
    const list = rail.querySelector(".artifact-rail-list");
    if (list) fillArtifactRail(list, alias);
  }
}

function renderArtifactAttachment(alias, parent, message) {
  const wrap = element("div");
  wrap.className = "artifact-attachment";
  if (!hasSurface("artifacts")) {
    wrap.append(element("p", `产物尚未开放 · ${text(message.id)}`));
    return wrap;
  }
  const artifactId = text(message && message.artifact_id);
  if (!SAFE_ID.test(artifactId)) {
    wrap.append(element("p", "产物不可用"));
    return wrap;
  }
  const artifact = state.artifactItems.find((item) => text(item.id) === artifactId);
  if (!artifact) {
    wrap.append(element("p", "产物不可用"));
    return wrap;
  }
  wrap.append(renderArtifactView(alias, parent, artifact));
  return wrap;
}

function renderArtifactView(alias, parent, artifact) {
  const card = element("div");
  card.className = "artifact-card";
  const kind = text(artifact && artifact.type);
  const title = text(artifact && artifact.title) || text(artifact && artifact.id);
  card.append(element("p", `${displayLabel(kind, ARTIFACT_TYPE_LABELS)} · ${title}`));
  if (kind === "review") {
    const conclusion = displayLabel(text(artifact && artifact.conclusion), REVIEW_CONCLUSION_LABELS);
    const bound = text(artifact && artifact.bound_hash).slice(0, 12);
    card.append(element("p", bound ? `${conclusion} · ${bound}` : conclusion));
    return card;
  }
  if (kind === "video") {
    const meta = [
      text(artifact && artifact.duration),
      count(artifact && artifact.size) ? `${count(artifact.size)} B` : "",
    ].filter(Boolean).join(" · ");
    if (meta) card.append(element("p", meta));
    const command = copyableOpenCommand(artifact && artifact.open_command);
    if (command) {
      card.append(commandRow(command));
    }
    return card;
  }
  if (kind === "chart") {
    const points = Array.isArray(artifact && artifact.points) ? artifact.points : [];
    if (points.length) {
      card.append(renderChartSvg(points));
      return card;
    }
  }
  if (kind === "image" || kind === "chart") {
    const img = document.createElement("img");
    img.alt = title;
    img.className = "artifact-image";
    bindArtifactImage(img, alias, parent, text(artifact && artifact.id));
    card.append(img);
    return card;
  }
  card.append(element("p", "产物不可用"));
  return card;
}

function renderChartSvg(points) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 200 80");
  svg.setAttribute("class", "artifact-chart");
  svg.setAttribute("role", "img");
  const xs = points.map((item) => Number(item.x)).filter((value) => Number.isFinite(value));
  const ys = points.map((item) => Number(item.y)).filter((value) => Number.isFinite(value));
  if (!xs.length || !ys.length) return svg;
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanX = maxX - minX || 1;
  const spanY = maxY - minY || 1;
  const coords = points.map((item) => {
    const x = 8 + ((Number(item.x) - minX) / spanX) * 184;
    const y = 72 - ((Number(item.y) - minY) / spanY) * 64;
    return `${x},${y}`;
  }).join(" ");
  const line = document.createElementNS(SVG_NS, "polyline");
  line.setAttribute("points", coords);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-width", "2");
  svg.append(line);
  return svg;
}

function bindArtifactImage(img, alias, parent, artifactId) {
  if (!SAFE_ID.test(alias) || !SAFE_ID.test(parent) || !SAFE_ID.test(artifactId)) {
    img.alt = "产物不可用";
    return;
  }
  const key = artifactRowKey(parent, artifactId);
  const cached = state.artifactBlobs.get(key);
  if (cached) {
    img.src = cached;
    return;
  }
  fetchArtifactBytes(alias, parent, artifactId).then((url) => {
    if (!url) {
      img.alt = "产物不可用";
      return;
    }
    state.artifactBlobs.set(key, url);
    img.src = url;
  }).catch((error) => {
    if (error && error.message === "SESSION_EXPIRED") expireSession();
    img.alt = "产物不可用";
  });
}

async function fetchArtifactBytes(alias, parent, artifactId) {
  const response = await fetch(
    `/api/v1/workspaces/${encodeURIComponent(alias)}/families/${encodeURIComponent(parent)}/artifacts/${encodeURIComponent(artifactId)}`,
    {
      headers: { Authorization: `Bearer ${state.bearer}` },
      cache: "no-store",
      credentials: "omit",
    },
  );
  if (response.status === 401) throw new Error("SESSION_EXPIRED");
  if (!response.ok) throw new Error("ARTIFACT_UNAVAILABLE");
  const type = text(response.headers.get("Content-Type"));
  if (!type.startsWith("image/")) throw new Error("ARTIFACT_UNAVAILABLE");
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}

async function loadArtifacts(alias, parent) {
  if (!hasSurface("artifacts") || document.hidden || !SAFE_ID.test(alias) || !SAFE_ID.test(parent)) {
    renderArtifactRails(alias);
    return;
  }
  const payload = await request(
    `/api/v1/workspaces/${encodeURIComponent(alias)}/families/${encodeURIComponent(parent)}/artifacts`,
    `artifacts:${alias}:${parent}`,
  );
  if (!payload || !payload.data) return;
  state.artifactItems = Array.isArray(payload.data.artifacts)
    ? payload.data.artifacts.filter((item) => item && SAFE_ID.test(text(item.id)))
    : [];
  renderArtifactRails(alias);
  renderChannelMessages();
}

function reloadOpenChannel() {
  const alias = state.detailAlias;
  const parent = state.familyParent;
  if (!alias || !parent) return;
  resetChannelState();
  loadChannel(alias, parent).catch((error) => {
    if (error && error.message === "SESSION_EXPIRED") expireSession();
  });
}

async function loadChannel(alias, parent) {
  if (!hasSurface("families") || document.hidden || !SAFE_ID.test(alias) || !SAFE_ID.test(parent)) {
    return;
  }
  const filter = channelFilterText();
  const params = new URLSearchParams();
  if (state.channelCursor) params.set("after", state.channelCursor);
  if (filter) params.set("filter", filter);
  const query = params.toString() ? `?${params.toString()}` : "";
  const key = `channel:${alias}:${parent}:${state.channelCursor || "0"}:${filter || "all"}`;
  const payload = await request(
    `/api/v1/workspaces/${encodeURIComponent(alias)}/families/${encodeURIComponent(parent)}/channel${query}`,
    key,
  );
  if (!payload || !payload.data) return;
  state.channelMembers = Array.isArray(payload.data.members) ? payload.data.members.map((item) => text(item)).filter(Boolean) : [];
  appendChannelMessages(Array.isArray(payload.data.messages) ? payload.data.messages : []);
  const cursor = text(payload.data.next_cursor);
  const received = Array.isArray(payload.data.messages) ? payload.data.messages.length : 0;
  state.channelCursor = received ? cursor : "";
  const more = $("channel-more");
  if (more) more.hidden = !state.channelCursor || !received;
  const from = $("channel-filter-from");
  if (from) {
    fillSelect(
      from,
      [["", "全部发送者"], ...state.channelMembers.map((id) => [id, id])],
      state.channelFrom,
    );
  }
  const composeTo = $("channel-post-to");
  if (composeTo) {
    fillSelect(
      composeTo,
      [["", "全家族"], ...state.channelMembers.filter((id) => id !== "operator").map((id) => [id, id])],
      text(composeTo.value),
    );
  }
  renderChannelMessages();
}

async function postHumanChannel(alias, parent, payload) {
  if (!SAFE_ID.test(alias) || !SAFE_ID.test(parent)) return;
  await requestWrite(
    `/api/v1/workspaces/${encodeURIComponent(alias)}/families/${encodeURIComponent(parent)}/channel`,
    payload,
  );
  resetChannelState();
  await loadChannel(alias, parent);
  await refreshFamilyUnread(alias, parent);
}

async function refreshFamilyUnread(alias, parent) {
  if (!hasSurface("families") || !SAFE_ID.test(alias) || !SAFE_ID.test(parent)) return;
  try {
    const payload = await request(
      `/api/v1/workspaces/${encodeURIComponent(alias)}/families/${encodeURIComponent(parent)}`,
      `family:${alias}:${parent}`,
    );
    const nodes = payload && payload.data && Array.isArray(payload.data.nodes) ? payload.data.nodes : [];
    const tree = $("family-tree");
    if (!tree) return;
    for (const node of nodes) {
      const unread = tree.querySelector(`[data-member="${text(node.id)}"] .family-unread`);
      if (unread) unread.textContent = `未读 ${count(node.unread)}`;
    }
  } catch (error) {
    if (error && error.message === "SESSION_EXPIRED") throw error;
  }
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
  root.append(renderFamilyTree(alias, lines, tasks), renderEventPane(), renderChannelPane(alias));
  return root;
}

function resetEventState() {
  stopEventLive();
  state.eventCursor = "";
  state.eventItems = [];
  state.liveEdges = new Set();
  state.sseFailed = false;
  resetChannelState();
  resetArtifactState();
}

async function loadWorkspace(alias, silent = false) {
  if (!SAFE_ID.test(alias)) return;
  if (state.detailAlias && state.detailAlias !== alias) {
    resetEventState();
  } else {
    resetChannelState();
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
    const room = element("div");
    room.className = "workspace-room";
    const meta = element("p");
    meta.className = "workspace-room-meta";
    meta.textContent = [
      text(summary.alias),
      displayLabel(summary.health, HEALTH_LABELS),
      displayLabel(summary.freshness, FRESHNESS_LABELS),
      displayLabel(summary.availability, AVAILABILITY_LABELS),
    ].filter(Boolean).join(" · ");
    const hero = element("div");
    hero.className = "workspace-hero";
    hero.append(renderLivePanes(alias, payload.data));
    const quiet = element("div");
    quiet.className = "workspace-quiet";
    quiet.append(grid, renderInventory(payload.data));
    const command = text(summary.recommendation && summary.recommendation.command);
    if (command) quiet.append(commandRow(command));
    quiet.append(await loadProofInspect(alias));
    room.append(meta, hero, renderOperatorTwin(payload.data), quiet);
    content.replaceChildren(room);
    renderEventList();
    renderChannelMessages();
    detail.hidden = false;
    $("detail-heading").focus();
    await startEventLive(alias);
    if (state.familyParent) {
      await refreshFamilyUnread(alias, state.familyParent);
      await loadChannel(alias, state.familyParent);
      await loadArtifacts(alias, state.familyParent);
    }
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

if (typeof document !== "undefined" && document.getElementById && document.getElementById("refresh")) {
  start();
}
globalThis.__dyroTwinLive = {
  twinFromData,
  renderOperatorTwin,
  mergeTwinFromEvents,
  applyLiveTwinEvents,
  renderedTwinText,
  emptyTwin,
  getState: () => state,
};
