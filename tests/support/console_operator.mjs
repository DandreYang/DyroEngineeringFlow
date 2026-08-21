import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const code = fs.readFileSync(
  path.join(rootDir, "src/dyro/console/assets/app.js"),
  "utf8",
);

function collect(node, out = []) {
  out.push(node);
  for (const child of node.children || []) collect(child, out);
  return out;
}

function matchSelector(node, selector) {
  if (selector.startsWith("#")) return node.id === selector.slice(1);
  if (selector.startsWith(".")) {
    return String(node.className).split(/\s+/).filter(Boolean).includes(selector.slice(1));
  }
  const attr = selector.match(/^(\w+)?\[([A-Za-z0-9_-]+)(?:="([^"]*)"|='([^']*)')?\]$/);
  if (attr) {
    const [, tag, key, double, single] = attr;
    const value = double === undefined ? single : double;
    if (tag && node.tagName !== tag.toUpperCase()) return false;
    if (key.startsWith("data-")) {
      const dataKey = key.slice(5);
      const current = node.dataset[dataKey];
      return value === undefined ? Boolean(current) : current === value;
    }
    const current = node.attrs[key];
    return value === undefined ? current != null && current !== "" : current === value;
  }
  return node.tagName === String(selector).toUpperCase();
}

function queryAll(scope, selector) {
  const parts = String(selector).trim().split(/\s+/).filter(Boolean);
  let current = [scope];
  for (const part of parts) {
    const next = [];
    for (const node of current) {
      for (const child of collect(node).slice(1)) {
        if (matchSelector(child, part)) next.push(child);
      }
    }
    current = next;
  }
  return current;
}

function collectText(node) {
  const bits = [];
  const walk = (item) => {
    if (!item) return;
    if (item.textContent) bits.push(String(item.textContent));
    for (const child of item.children || []) walk(child);
  };
  walk(node);
  return bits.join("\n");
}

function fakeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    className: "",
    id: "",
    hidden: false,
    type: "",
    textContent: "",
    disabled: false,
    dataset: {},
    attrs: {},
    listeners: {},
    classList: {
      add(...names) {
        const current = new Set(String(node.className).split(/\s+/).filter(Boolean));
        for (const name of names) current.add(name);
        node.className = [...current].join(" ");
      },
      toggle(name, force) {
        const current = new Set(String(node.className).split(/\s+/).filter(Boolean));
        const on = force === undefined ? !current.has(name) : Boolean(force);
        if (on) current.add(name);
        else current.delete(name);
        node.className = [...current].join(" ");
      },
    },
    get firstChild() {
      return node.children[0] || null;
    },
    get lastChild() {
      return node.children.length ? node.children[node.children.length - 1] : null;
    },
    append(...items) {
      for (const item of items) node.children.push(item);
    },
    addEventListener(type, fn) {
      node.listeners[type] = node.listeners[type] || [];
      node.listeners[type].push(fn);
    },
    click() {
      return Promise.all((node.listeners.click || []).map((fn) => fn({ target: node })));
    },
    replaceWith() {},
    replaceChildren(...items) {
      node.children = items.slice();
    },
    setAttribute(name, value) {
      node.attrs[name] = String(value);
      if (name === "hidden") node.hidden = true;
      if (name.startsWith("data-")) node.dataset[name.slice(5)] = String(value);
    },
    removeAttribute(name) {
      delete node.attrs[name];
      if (name === "hidden") node.hidden = false;
      if (name.startsWith("data-")) delete node.dataset[name.slice(5)];
    },
    getAttribute(name) {
      if (name === "hidden") return node.hidden ? "" : null;
      return Object.prototype.hasOwnProperty.call(node.attrs, name) ? node.attrs[name] : null;
    },
    querySelector(selector) {
      return queryAll(node, selector)[0] || null;
    },
    querySelectorAll(selector) {
      return queryAll(node, selector);
    },
    focus() {},
  };
  return node;
}

const nodesById = new Map();
function seed(id) {
  const node = fakeNode("div");
  node.id = id;
  nodesById.set(id, node);
  return node;
}

const commandCenter = seed("command-center");
commandCenter.className = "command-center";
for (const id of [
  "overview-heading",
  "overview-summary",
  "captured-at",
  "attention-counts",
  "needs-you",
  "primary-guidance",
  "primary-why",
  "primary-command",
  "primary-copy",
  "task-status-counts",
  "workspace-list",
  "session-status",
  "system-panel",
  "system-note",
  "system-update",
  "refresh",
  "detail-close",
  "workspace-detail",
]) {
  const node = seed(id);
  if (
    [
      "overview-heading",
      "overview-summary",
      "needs-you",
      "primary-guidance",
      "primary-why",
      "primary-command",
      "primary-copy",
    ].includes(id)
  ) {
    commandCenter.append(node);
  }
}
nodesById.get("overview-heading").textContent = "正在读取工程状态";
nodesById.get("overview-summary").textContent = "正在读取本地工作区状态。";
nodesById.get("primary-command").textContent = "正在准备推荐命令…";
nodesById.get("session-status").textContent = "正在建立安全本地会话…";

function resetCommandCenterPlaceholders() {
  const heading = nodesById.get("overview-heading");
  const summary = nodesById.get("overview-summary");
  const command = nodesById.get("primary-command");
  const copy = nodesById.get("primary-copy");
  heading.textContent = "正在读取工程状态";
  summary.textContent = "正在读取本地工作区状态。";
  command.textContent = "正在准备推荐命令…";
  command.hidden = false;
  copy.hidden = false;
  copy.disabled = true;
  copy.dataset.command = "";
}

function pickerLabels(pane) {
  const nav = pane && pane.querySelector ? pane.querySelector(".family-picker") : null;
  if (!nav) return [];
  return (nav.querySelectorAll("button") || []).map((button) => button.textContent);
}

const fetchCalls = [];
let fetchImpl = async () => ({
  ok: false,
  status: 404,
  headers: { get: () => null },
  json: async () => null,
});

const context = vm.createContext({
  console,
  Set,
  Map,
  JSON,
  Date,
  Number,
  Boolean,
  String,
  Array,
  Object,
  Math,
  Error,
  TextDecoder,
  URLSearchParams,
  document: {
    hidden: false,
    getElementById: (id) => nodesById.get(id) || null,
    createElement: (name) => fakeNode(name),
    createTextNode: (value) => {
      const node = fakeNode("#text");
      node.textContent = String(value);
      return node;
    },
    addEventListener() {},
  },
  window: {
    location: { hash: "", pathname: "/", search: "" },
    history: { replaceState() {} },
    setTimeout() {},
    clearTimeout() {},
    addEventListener() {},
  },
  sessionStorage: {
    getItem() {
      return null;
    },
    setItem() {},
    removeItem() {},
  },
  fetch: async (path, init = {}) => {
    fetchCalls.push({ path, headers: { ...(init.headers || {}) }, method: init.method || "GET" });
    return fetchImpl(path, init);
  },
  navigator: { clipboard: { writeText: async () => {} } },
});
context.globalThis = context;
vm.runInContext(code, context);

const api = context.__dyroConsoleTest;
if (!api || typeof api.renderLivePanes !== "function") {
  throw new Error("console operator test API is not loaded");
}

function emptyAttention() {
  return {
    repair_required: 0,
    needs_user: 0,
    ready: 0,
    paused: 0,
    waiting: 0,
  };
}

function visiblePaneIds(root) {
  return (root.querySelectorAll(".live-pane") || [])
    .filter((pane) => !pane.hidden)
    .map((pane) => pane.id);
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const action = input.action;
let result = {};

if (action === "fail_overview") {
  api.renderOverview({
    captured_at: "2026-08-21T07:00:00Z",
    freshness: { partial: false, warnings: [] },
    data: {
      total_workspaces: 1,
      attention_counts: emptyAttention(),
      task_status_counts: {},
      workspaces: [
        {
          alias: "core",
          display_name: "core",
          availability: "available",
          health: "degraded",
          findings: [
            { status: "FAIL", reason: "MISSING_ORIGIN", line: "core" },
            { status: "FAIL", reason: "MISSING_ORIGIN", line: "core_pay" },
            { status: "FAIL", reason: "MISSING_ORIGIN", line: "release_a" },
          ],
          recommendation: { reason: "HOME_GUIDANCE", command: "dyro --workspace core" },
          attention_counts: emptyAttention(),
        },
      ],
    },
  });
  result = {
    heading: nodesById.get("overview-heading").textContent,
    primary: nodesById.get("primary-command").textContent,
    command: nodesById.get("primary-copy").dataset.command,
    needsYou: collectText(nodesById.get("needs-you")),
  };
} else if (action === "tabs") {
  const live = api.renderLivePanes("core", {
    workspace: { findings: [] },
    lines: [
      { id: "core", parent: "" },
      { id: "core_pay", parent: "core" },
    ],
    tasks: [],
  });
  const tablist = live.querySelector('[role="tablist"]');
  const buttons = tablist ? tablist.querySelectorAll("button[data-tab]") : [];
  const seen = [];
  for (const button of buttons) {
    button.click();
    seen.push({
      tab: button.dataset.tab,
      hashTab: api.getState().detailTab,
      visible: visiblePaneIds(live),
    });
  }
  result = {
    tabs: buttons.map((button) => button.dataset.tab),
    switches: seen,
  };
} else if (action === "spawn") {
  const empty = api.renderFamilyGraph("core", [{ id: "core", parent: "" }], "core", [], []);
  const withChild = api.renderFamilyGraph(
    "core",
    [
      { id: "core", parent: "" },
      { id: "core_pay", parent: "core" },
    ],
    "core",
    [],
    [],
  );
  result = {
    empty: collectText(empty),
    withChild: collectText(withChild),
  };
} else if (action === "badges") {
  const fail = api.familyBadges("core", [], 0, [
    { status: "FAIL", reason: "MISSING_ORIGIN", line: "core" },
  ]);
  const unknown = api.familyBadges("core_pay", [], 0, [
    { status: "FAIL", reason: "MISSING_ORIGIN", line: "core" },
  ]);
  result = {
    fail: collectText(fail),
    unknown: collectText(unknown),
  };
} else if (action === "refresh") {
  const coreCard = {
    alias: "core",
    display_name: "core",
    availability: "available",
    health: "healthy",
    findings: [],
    recommendation: { reason: "HOME_GUIDANCE", command: "dyro --workspace core doctor" },
    attention_counts: emptyAttention(),
    unavailable_reason: "",
  };
  const freshOverview = {
    captured_at: "2026-08-21T08:01:00Z",
    freshness: { partial: false, warnings: [] },
    data: {
      total_workspaces: 1,
      attention_counts: emptyAttention(),
      task_status_counts: {},
      workspaces: [coreCard],
    },
  };
  api.getState().bearer = "token";
  api.getState().etags.set("overview", '"old-etag"');
  fetchImpl = async () => ({
    ok: true,
    status: 200,
    headers: { get: (name) => (name === "ETag" ? '"new-etag"' : null) },
    json: async () => ({ captured_at: "2026-08-21T08:00:00Z" }),
  });
  await api.request("/api/v1/overview?limit=100", "overview");
  const cached = { ...fetchCalls[0] };
  fetchCalls.length = 0;
  await api.request("/api/v1/overview?limit=100", "overview", { force: true });
  const forced = { ...fetchCalls[0] };
  api.renderOverview({
    captured_at: "2026-08-21T07:00:00Z",
    freshness: { partial: false, warnings: [] },
    data: {
      total_workspaces: 1,
      attention_counts: emptyAttention(),
      task_status_counts: {},
      workspaces: [coreCard],
    },
  });
  const before = nodesById.get("captured-at").textContent;
  fetchImpl = async (_path, init = {}) => {
    if (init.headers && init.headers["If-None-Match"]) {
      return {
        ok: true,
        status: 304,
        headers: { get: () => null },
        json: async () => null,
      };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: (name) => (name === "ETag" ? '"refreshed-etag"' : null) },
      json: async () => freshOverview,
    };
  };
  fetchCalls.length = 0;
  await api.refresh({ force: true });
  const refreshed = { ...fetchCalls[0] };
  const afterRefresh = nodesById.get("captured-at").textContent;
  api.renderOverview({
    captured_at: "2026-08-21T07:00:00Z",
    freshness: { partial: false, warnings: [] },
    data: {
      total_workspaces: 1,
      attention_counts: emptyAttention(),
      task_status_counts: {},
      workspaces: [coreCard],
    },
  });
  fetchCalls.length = 0;
  await nodesById.get("refresh").click();
  const clicked = { ...fetchCalls[0] };
  result = {
    cachedHasMatch: Boolean(cached.headers["If-None-Match"]),
    forcedHasMatch: Boolean(forced.headers["If-None-Match"]),
    refreshHasMatch: Boolean(refreshed.headers["If-None-Match"]),
    clickHasMatch: Boolean(clicked.headers["If-None-Match"]),
    before,
    afterRefresh,
    afterClick: nodesById.get("captured-at").textContent,
    after: afterRefresh,
  };
} else if (action === "session") {
  resetCommandCenterPlaceholders();
  context.window.location.hash = "";
  await api.start();
  const missingCenter = collectText(commandCenter);
  const missing = {
    status: nodesById.get("session-status").textContent,
    heading: nodesById.get("overview-heading").textContent,
    primary: nodesById.get("primary-command").textContent,
    primaryHidden: Boolean(nodesById.get("primary-command").hidden),
    center: missingCenter,
    helper: api.sessionMissingMessage(),
  };
  resetCommandCenterPlaceholders();
  context.window.location.hash = "#bootstrap=dead-token";
  fetchImpl = async () => ({
    ok: false,
    status: 401,
    headers: { get: () => null },
    json: async () => ({}),
  });
  await api.start();
  const expiredCenter = collectText(commandCenter);
  const expired = {
    status: nodesById.get("session-status").textContent,
    heading: nodesById.get("overview-heading").textContent,
    primary: nodesById.get("primary-command").textContent,
    primaryHidden: Boolean(nodesById.get("primary-command").hidden),
    center: expiredCenter,
    helper: api.sessionExpiredMessage(),
  };
  result = { missing, expired, missingText: missing.helper, expiredText: expired.helper };
} else if (action === "empty") {
  const graph = api.renderFamilyGraph("core", [], "core", [], []);
  result = { text: collectText(graph) };
} else if (action === "ghost_overview") {
  const ghostRepair = {
    alias: "test-workspace",
    display_name: "test-workspace",
    availability: "unavailable",
    health: "unavailable",
    findings: [{ status: "FAIL", reason: "MISSING_ORIGIN", line: "core" }],
    recommendation: {
      reason: "MISSING_ORIGIN",
      command: "dyro --workspace test-workspace doctor",
    },
    attention_counts: {
      repair_required: 1,
      needs_user: 0,
      ready: 0,
      paused: 0,
      waiting: 0,
    },
    unavailable_reason: "missing_root",
  };
  const core = {
    alias: "core",
    display_name: "core",
    availability: "available",
    health: "healthy",
    findings: [],
    recommendation: { reason: "HOME_GUIDANCE", command: "dyro --workspace core doctor" },
    attention_counts: emptyAttention(),
    unavailable_reason: "",
  };
  api.renderOverview({
    captured_at: "2026-08-21T07:00:00Z",
    freshness: { partial: true, warnings: [] },
    data: {
      total_workspaces: 2,
      attention_counts: emptyAttention(),
      task_status_counts: {},
      workspaces: [core, ghostRepair],
    },
  });
  result = {
    heading: nodesById.get("overview-heading").textContent,
    primary: nodesById.get("primary-command").textContent,
    command: nodesById.get("primary-copy").dataset.command,
    why: nodesById.get("primary-why").textContent,
    needsYou: collectText(nodesById.get("needs-you")),
    list: collectText(nodesById.get("workspace-list")),
    needsYouAliases: api.needsYouWorkspaces([core, ghostRepair]).map((item) => item.alias),
    ghostCommand: api.recommendedCommand(ghostRepair),
    priorityAlias: (api.priorityWorkspace([core, ghostRepair]) || {}).alias || "",
  };
} else if (action === "timeout_overview") {
  const timeout = {
    alias: "slow",
    display_name: "slow",
    availability: "unavailable",
    health: "unavailable",
    findings: [],
    recommendation: { reason: "WORKSPACE_TIMEOUT", command: "dyro --workspace slow doctor" },
    attention_counts: emptyAttention(),
    unavailable_reason: "read_timeout",
  };
  const missing = {
    alias: "test-workspace",
    display_name: "test-workspace",
    availability: "unavailable",
    health: "unavailable",
    findings: [],
    recommendation: {
      reason: "WORKSPACE_MISSING_ROOT",
      command: "dyro --workspace test-workspace doctor",
    },
    attention_counts: emptyAttention(),
    unavailable_reason: "missing_root",
  };
  const core = {
    alias: "core",
    display_name: "core",
    availability: "available",
    health: "healthy",
    findings: [],
    recommendation: { reason: "HOME_GUIDANCE", command: "dyro --workspace core doctor" },
    attention_counts: emptyAttention(),
    unavailable_reason: "",
  };
  api.renderOverview({
    captured_at: "2026-08-21T07:00:00Z",
    freshness: { partial: true, warnings: [] },
    data: {
      total_workspaces: 3,
      attention_counts: emptyAttention(),
      task_status_counts: {},
      workspaces: [timeout, missing, core],
    },
  });
  result = {
    heading: nodesById.get("overview-heading").textContent,
    command: nodesById.get("primary-copy").dataset.command,
    needsYou: collectText(nodesById.get("needs-you")),
    list: collectText(nodesById.get("workspace-list")),
    timeoutMatter: api.workspaceMatter(timeout),
    missingMatter: api.workspaceMatter(missing),
    timeoutReason: api.unavailableReason(timeout),
    missingReason: api.unavailableReason(missing),
  };
} else if (action === "family_picker") {
  const lines = [
    { id: "core", parent: "" },
    { id: "core_pay", parent: "core" },
    { id: "core_pay_fix", parent: "core_pay" },
    { id: "release_a", parent: "" },
  ];
  api.getState().surfaces = ["events"];
  api.getState().familyParent = "";
  const roots = api.familyParents(lines);
  const rootButtons = pickerLabels(api.renderFamilyTree("core", lines, [], []));
  api.getState().familyParent = "core_pay";
  const focused = api.familyParents(lines);
  const focusedButtons = pickerLabels(api.renderFamilyTree("core", lines, [], []));
  api.getState().familyParent = "core_pay_fix";
  const grandchild = api.familyParents(lines);
  const grandchildButtons = pickerLabels(api.renderFamilyTree("core", lines, [], []));
  result = {
    roots,
    focused,
    grandchild,
    rootButtons,
    focusedButtons,
    grandchildButtons,
  };
} else if (action === "fail_over_ready") {
  const attention = {
    repair_required: 0,
    needs_user: 0,
    ready: 1,
    paused: 0,
    waiting: 0,
  };
  const workspaces = [
    {
      alias: "core",
      display_name: "core",
      availability: "available",
      health: "degraded",
      findings: [{ status: "FAIL", reason: "MISSING_ORIGIN", line: "core" }],
      recommendation: { reason: "MISSING_ORIGIN", command: "dyro --workspace core doctor" },
      attention_counts: attention,
      unavailable_reason: "",
    },
  ];
  api.renderOverview({
    captured_at: "2026-08-21T07:00:00Z",
    freshness: { partial: false, warnings: [] },
    data: {
      total_workspaces: 1,
      attention_counts: attention,
      task_status_counts: {},
      workspaces,
    },
  });
  result = {
    heading: nodesById.get("overview-heading").textContent,
    state: api.overviewState(attention, workspaces),
  };
} else if (action === "hash_tab") {
  context.window.location.hash = "#w/core/events";
  api.getState().detailTab = "events";
  const live = api.renderLivePanes("core", {
    workspace: { findings: [] },
    lines: [
      { id: "core", parent: "" },
      { id: "core_pay", parent: "core" },
    ],
    tasks: [],
  });
  result = {
    visible: visiblePaneIds(live),
    detailTab: api.getState().detailTab,
  };
} else if (action === "empty_twin") {
  const twinApi = context.__dyroTwinLive;
  const twin = twinApi.renderOperatorTwin({
    lines: [
      { id: "core", parent: "" },
      { id: "core_pay", parent: "core" },
    ],
    tasks: [],
    objectives: [],
    operator_twin: {
      plan: [],
      phases: [],
      running: [],
      latest_ledger: { present: false, at: "", task_id: "", phase: "", facts: {} },
      projected_seq: 0,
      overlay_complete: true,
    },
  });
  result = { text: collectText(twin) };
} else {
  throw new Error(`unknown action ${action}`);
}

process.stdout.write(JSON.stringify(result));
