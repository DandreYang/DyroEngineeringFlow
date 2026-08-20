import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const code = fs.readFileSync(
  path.join(root, "src/dyro/console/assets/app.js"),
  "utf8",
);

function fakeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    className: "",
    id: "",
    hidden: false,
    type: "",
    textContent: "",
    dataset: {},
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
    append(...items) {
      for (const item of items) node.children.push(item);
    },
    addEventListener() {},
    replaceWith() {},
    replaceChildren(...items) {
      node.children = items.slice();
    },
  };
  return node;
}

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
    getElementById: () => null,
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
  fetch: async () => ({ ok: false, status: 404, json: async () => null }),
});
context.globalThis = context;
vm.runInContext(code, context);

const api = context.__dyroTwinLive;
if (
  !api
  || typeof api.mergeTwinFromEvents !== "function"
  || typeof api.twinFromData !== "function"
  || typeof api.renderOperatorTwin !== "function"
) {
  throw new Error("renderOperatorTwin / mergeTwinFromEvents is not loaded");
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const snapshot = input.snapshot || {};
const events = Array.isArray(input.events) ? input.events : [];
api.renderOperatorTwin(snapshot);
api.mergeTwinFromEvents(events);
const twin = api.getState().operatorTwin;
const done = (twin.phases.find((column) => column.status === "done") || { tasks: [] }).tasks.map(
  (task) => task.id,
);
process.stdout.write(
  JSON.stringify({
    running: twin.running,
    done_ids: done,
    plan_ids: (twin.plan || []).map((row) => row.id),
    rendered: api.renderedTwinText(twin),
    board_landed: (twin.running || []).some((row) => row.board_landed),
    wave_ids: (twin.plan || []).filter((row) => row.wave_present).map((row) => row.id),
    projected_seq: twin.projected_seq,
    overlay_complete: twin.overlay_complete === true,
    after_seq: api.getState().twinAfterSeq,
  }),
);
