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
      for (const fn of node.listeners.click || []) fn({ target: node });
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
]) {
  seed(id);
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
    data: {
      total_workspaces: 1,
      attention_counts: emptyAttention(),
      task_status_counts: {},
      workspaces: [
        {
          alias: "core",
          display_name: "core",
          availability: "available",
          health: "healthy",
          findings: [],
          recommendation: { reason: "HOME_GUIDANCE", command: "dyro --workspace core doctor" },
          attention_counts: emptyAttention(),
        },
      ],
    },
  });
  const before = nodesById.get("captured-at").textContent;
  api.renderOverview({
    captured_at: "2026-08-21T08:01:00Z",
    data: {
      total_workspaces: 1,
      attention_counts: emptyAttention(),
      task_status_counts: {},
      workspaces: [
        {
          alias: "core",
          display_name: "core",
          availability: "available",
          health: "healthy",
          findings: [],
          recommendation: { reason: "HOME_GUIDANCE", command: "dyro --workspace core doctor" },
          attention_counts: emptyAttention(),
        },
      ],
    },
  });
  result = {
    cachedHasMatch: Boolean(cached.headers["If-None-Match"]),
    forcedHasMatch: Boolean(forced.headers["If-None-Match"]),
    before,
    after: nodesById.get("captured-at").textContent,
  };
} else if (action === "session") {
  result = {
    missing: api.sessionMissingMessage(),
    expired: api.sessionExpiredMessage(),
  };
} else if (action === "empty") {
  const graph = api.renderFamilyGraph("core", [], "core", [], []);
  result = { text: collectText(graph) };
} else {
  throw new Error(`unknown action ${action}`);
}

process.stdout.write(JSON.stringify(result));
