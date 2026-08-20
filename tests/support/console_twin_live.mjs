import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const code = fs.readFileSync(
  path.join(root, "src/dyro/console/assets/app.js"),
  "utf8",
);

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
if (!api || typeof api.mergeTwinFromEvents !== "function" || typeof api.twinFromData !== "function") {
  throw new Error("mergeTwinFromEvents is not loaded");
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const snapshot = input.snapshot || {};
const events = Array.isArray(input.events) ? input.events : [];
const state = api.getState();
state.twinTasks = Array.isArray(snapshot.tasks) ? snapshot.tasks : [];
state.operatorTwin = api.twinFromData(snapshot);
if (input.overlay_complete === false) {
  state.operatorTwin.overlay_complete = false;
}
if (Number.isInteger(input.after_seq)) {
  state.operatorTwin.projected_seq = input.after_seq;
}
state.twinAfterSeq = state.operatorTwin.projected_seq;
state.twinOverlayComplete = state.operatorTwin.overlay_complete === true;
api.mergeTwinFromEvents(events);
const twin = state.operatorTwin;
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
  }),
);
