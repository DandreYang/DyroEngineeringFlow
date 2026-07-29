/**
 * Stage 4 Agent Broker — integrity-pinned argv CLI + raw capture on tmpfs.
 *
 * Modes:
 * - fake: deterministic text, no CLI
 * - simulated-cli: in-process raw file (Stage 2 compatibility)
 * - argv-cli: spawn pinned argv provider, capture stdout/stderr to tmpfs, destroy
 *
 * Provider credentials (DYRO_PROVIDER_FAKE_TOKEN) exist only in this process.
 * Execution keys must never appear in the Broker environment.
 * Provider script/binary content must match DYRO_PROVIDER_ARGV_SHA256.
 */
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { createServer, type Socket } from "node:net";
import { join } from "node:path";

const port = Number(process.env.DYRO_BROKER_PORT ?? "7421");
const host = process.env.DYRO_BROKER_HOST ?? "127.0.0.1";
const allowedModel = process.env.DYRO_BROKER_MODEL ?? "fake-model";
const telemetryPath = process.env.DYRO_BROKER_TELEMETRY_PATH ?? "";
const providerMode = process.env.DYRO_PROVIDER_MODE ?? "argv-cli";
const maxConcurrency = Math.max(
  1,
  Number(process.env.DYRO_BROKER_MAX_CONCURRENCY ?? "2"),
);
const rawRoot = process.env.DYRO_PROVIDER_RAW_ROOT ?? "/tmp/provider-raw";
const providerArgv = (
  process.env.DYRO_PROVIDER_ARGV ?? "bun,/opt/workflow/fake_provider_cli.ts"
).split(",").filter(Boolean);
const providerPinPath =
  process.env.DYRO_PROVIDER_PIN_PATH ?? "/opt/workflow/fake_provider_cli.ts";
const providerArgvSha256 = process.env.DYRO_PROVIDER_ARGV_SHA256 ?? "";
const RAW_MARKER = "RAW_VENDOR_SECRET_MUST_NOT_ESCAPE";
const STDERR_MARKER = "RAW_VENDOR_STDERR_MARKER";

if (process.env.DYRO_EXECUTION_KEY || process.env.DYRO_EXECUTION_KEY_MATERIAL) {
  process.stderr.write("broker-refuses-execution-key\n");
  process.exit(3);
}

function verifyProviderPin(): void {
  if (providerMode !== "argv-cli") {
    return;
  }
  if (!providerArgvSha256 || providerArgvSha256.length !== 64) {
    process.stderr.write("broker-pin-mismatch missing-or-invalid-sha256\n");
    process.exit(4);
  }
  if (!existsSync(providerPinPath)) {
    process.stderr.write("broker-pin-mismatch missing-provider-file\n");
    process.exit(4);
  }
  const digest = createHash("sha256")
    .update(readFileSync(providerPinPath))
    .digest("hex");
  if (digest !== providerArgvSha256) {
    process.stderr.write(
      `broker-pin-mismatch expected=${providerArgvSha256} got=${digest}\n`,
    );
    process.exit(4);
  }
}

verifyProviderPin();

type Request = {
  protocol_version: number;
  type: "agent.call";
  call_id: string;
  prompt: string;
  model: string;
  cwd: string;
  deadline_ms: number;
  schema_hint?: string;
};

function sanitize(text: string): string {
  return text
    .replaceAll("BEGIN PRIVATE KEY", "[REDACTED]")
    .replaceAll("execution-key", "[REDACTED]")
    .replaceAll("DYRO_EXECUTION_KEY", "[REDACTED]")
    .replaceAll(RAW_MARKER, "[REDACTED]")
    .replaceAll(STDERR_MARKER, "[REDACTED]")
    .replace(/sk-[A-Za-z0-9-]+/g, "[REDACTED]")
    .replace(/token=[^\n]+/g, "token=[REDACTED]")
    .slice(0, 512);
}

function appendTelemetry(event: Record<string, unknown>): void {
  if (!telemetryPath) {
    return;
  }
  appendFileSync(telemetryPath, `${JSON.stringify(event)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
}

class Semaphore {
  #permits: number;
  #queue: Array<() => void> = [];
  constructor(permits: number) {
    this.#permits = permits;
  }
  async acquire(): Promise<void> {
    if (this.#permits > 0) {
      this.#permits -= 1;
      return;
    }
    await new Promise<void>((resolve) => this.#queue.push(resolve));
  }
  release(): void {
    const next = this.#queue.shift();
    if (next) {
      next();
      return;
    }
    this.#permits += 1;
  }
}

const semaphore = new Semaphore(maxConcurrency);
let activeCalls = 0;
let maxObservedConcurrency = 0;

function ensureRawRoot(): void {
  mkdirSync(rawRoot, { recursive: true, mode: 0o700 });
}

function rawPaths(callId: string): { out: string; err: string } {
  return {
    out: join(rawRoot, `${callId}.stdout`),
    err: join(rawRoot, `${callId}.stderr`),
  };
}

function destroyRaw(callId: string): void {
  const { out, err } = rawPaths(callId);
  for (const path of [out, err, join(rawRoot, `${callId}.raw`)]) {
    if (existsSync(path)) {
      rmSync(path, { force: true });
    }
  }
}

function assertNoRawResidue(): void {
  if (!existsSync(rawRoot)) {
    return;
  }
  const leftovers = readdirSync(rawRoot);
  if (leftovers.length > 0) {
    throw new Error(`raw provider residue remains: ${leftovers.join(",")}`);
  }
}

function providerEnv(): Record<string, string> {
  // Explicit allowlist only — never inherit host secrets wholesale.
  const env: Record<string, string> = {
    PATH: process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin",
    HOME: "/tmp",
    TMPDIR: "/tmp",
    LANG: "C",
  };
  if (process.env.DYRO_PROVIDER_FAKE_TOKEN) {
    env.DYRO_PROVIDER_FAKE_TOKEN = process.env.DYRO_PROVIDER_FAKE_TOKEN;
  }
  return env;
}

async function runArgvCli(request: Request): Promise<{
  status: "ok" | "error";
  text: string;
  error_code: string;
}> {
  if (providerArgv.length === 0) {
    return { status: "error", text: "", error_code: "provider_argv_empty" };
  }
  ensureRawRoot();
  const { out, err } = rawPaths(request.call_id);
  const proc = Bun.spawn([...providerArgv, request.prompt], {
    cwd: "/tmp",
    env: providerEnv(),
    stdout: "pipe",
    stderr: "pipe",
    stdin: "ignore",
  });
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ]);
  writeFileSync(out, stdout, { encoding: "utf8", mode: 0o600 });
  writeFileSync(err, stderr, { encoding: "utf8", mode: 0o600 });
  try {
    if (exitCode !== 0) {
      return { status: "error", text: "", error_code: `cli_exit_${exitCode}` };
    }
    // Prove files existed as raw capture before destroy.
    const rawOut = readFileSync(out, "utf8");
    const rawErr = readFileSync(err, "utf8");
    if (!rawOut.includes(RAW_MARKER) && !rawErr.includes(STDERR_MARKER)) {
      // still sanitize whatever we got
    }
    const summaryLine =
      rawOut
        .split("\n")
        .find((line) => line.startsWith("final:"))
        ?.slice("final:".length) ?? rawOut.slice(0, 120);
    const text = sanitize(
      `argv-cli:${request.model}:${summaryLine}:${request.prompt.trim().slice(0, 60)}`,
    );
    if (
      text.includes(RAW_MARKER) ||
      text.includes(STDERR_MARKER) ||
      text.includes("BEGIN PRIVATE KEY") ||
      text.includes("sk-stage4") ||
      text.includes("sk-stage3")
    ) {
      return { status: "error", text: "", error_code: "sanitizer_failed" };
    }
    return { status: "ok", text, error_code: "" };
  } finally {
    destroyRaw(request.call_id);
  }
}

async function runProvider(request: Request): Promise<{
  status: "ok" | "error";
  text: string;
  error_code: string;
}> {
  if (request.model !== allowedModel) {
    return { status: "error", text: "", error_code: "model_not_allowed" };
  }
  if (request.protocol_version !== 1 && request.protocol_version !== 2) {
    return { status: "error", text: "", error_code: "unsupported_protocol" };
  }
  if (request.protocol_version === 1 && request.schema_hint) {
    return { status: "error", text: "", error_code: "v1_rejects_schema_hint" };
  }
  if (providerMode === "fake") {
    return {
      status: "ok",
      text: sanitize(
        `fake-provider:${request.model}:${request.prompt.trim().slice(0, 120)}`,
      ),
      error_code: "",
    };
  }
  if (providerMode === "simulated-cli") {
    ensureRawRoot();
    const path = join(rawRoot, `${request.call_id}.raw`);
    writeFileSync(
      path,
      ["BEGIN PRIVATE KEY", RAW_MARKER, request.prompt].join("\n"),
      { encoding: "utf8", mode: 0o600 },
    );
    try {
      await Bun.sleep(30);
      return {
        status: "ok",
        text: sanitize(
          `simulated-cli:${request.model}:${request.prompt.trim().slice(0, 80)}`,
        ),
        error_code: "",
      };
    } finally {
      destroyRaw(request.call_id);
    }
  }
  if (providerMode === "argv-cli") {
    return runArgvCli(request);
  }
  return { status: "error", text: "", error_code: "unknown_provider_mode" };
}

async function handleLine(line: string, socket: Socket): Promise<void> {
  const started = Date.now();
  let response: Record<string, unknown>;
  let rawDestroyed = true;
  try {
    const request = JSON.parse(line) as Request;
    if (
      !request ||
      request.type !== "agent.call" ||
      !request.call_id ||
      !request.prompt
    ) {
      throw new Error("invalid_request");
    }
    await semaphore.acquire();
    activeCalls += 1;
    maxObservedConcurrency = Math.max(maxObservedConcurrency, activeCalls);
    try {
      const result = await runProvider(request);
      destroyRaw(request.call_id);
      rawDestroyed =
        !existsSync(rawPaths(request.call_id).out) &&
        !existsSync(rawPaths(request.call_id).err) &&
        !existsSync(join(rawRoot, `${request.call_id}.raw`));
      response = {
        protocol_version: request.protocol_version,
        type: "agent.result",
        call_id: request.call_id,
        status: result.status,
        text: result.text,
        error_code: result.error_code,
      };
      appendTelemetry({
        schema_version: 1,
        kind: "agent_call",
        call_id: request.call_id,
        model: request.model,
        status: result.status,
        error_code: result.error_code,
        duration_ms: Date.now() - started,
        prompt_chars: request.prompt.length,
        response_chars: result.text.length,
        provider_mode: providerMode,
        protocol_version: request.protocol_version,
        raw_destroyed: rawDestroyed,
        max_observed_concurrency: maxObservedConcurrency,
        sanitizer: "stage4-v1",
        provider_pin_verified: providerMode === "argv-cli",
      });
    } finally {
      activeCalls -= 1;
      semaphore.release();
    }
  } catch {
    response = {
      protocol_version: 1,
      type: "agent.result",
      call_id: "invalid",
      status: "error",
      text: "",
      error_code: "invalid_request",
    };
  }
  socket.write(`${JSON.stringify(response)}\n`);
  socket.end();
}

const server = createServer((socket) => {
  let buffer = "";
  socket.setEncoding("utf8");
  socket.on("data", (chunk: string) => {
    buffer += chunk;
    const newline = buffer.indexOf("\n");
    if (newline === -1) {
      return;
    }
    const line = buffer.slice(0, newline).trim();
    buffer = buffer.slice(newline + 1);
    void handleLine(line, socket);
  });
});

server.listen(port, host, () => {
  ensureRawRoot();
  const pinNote =
    providerMode === "argv-cli" ? ` pin=${providerArgvSha256.slice(0, 12)}` : "";
  const line = `broker-ready ${host}:${port} mode=${providerMode} concurrency=${maxConcurrency}${pinNote}\n`;
  process.stdout.write(line);
  process.stderr.write(line);
});

function shutdown(): void {
  try {
    assertNoRawResidue();
    appendTelemetry({
      schema_version: 1,
      kind: "broker_shutdown",
      max_observed_concurrency: maxObservedConcurrency,
      provider_mode: providerMode,
      raw_residue: false,
    });
  } catch (error) {
    process.stderr.write(`broker-shutdown-error ${String(error)}\n`);
    process.exitCode = 2;
  }
  server.close(() => process.exit(process.exitCode ?? 0));
  setTimeout(() => process.exit(process.exitCode ?? 0), 500).unref();
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
