/**
 * Stage 2 Agent Broker — TCP JSON-line protocol v1/v2.
 *
 * Provider modes:
 * - fake: deterministic sanitized text (no raw materialization)
 * - simulated-cli: writes raw vendor-like output only under /tmp/provider-raw
 *   (tmpfs), returns sanitized text, then destroys the raw file before reply
 *
 * Credentials never enter the Workflow Sandbox. Concurrency is limited by a
 * simple in-process semaphore.
 */
import {
  appendFileSync,
  mkdirSync,
  readdirSync,
  rmSync,
  writeFileSync,
  existsSync,
} from "node:fs";
import { createServer, type Socket } from "node:net";
import { join } from "node:path";

const port = Number(process.env.DYRO_BROKER_PORT ?? "7421");
const host = process.env.DYRO_BROKER_HOST ?? "127.0.0.1";
const allowedModel = process.env.DYRO_BROKER_MODEL ?? "fake-model";
const telemetryPath = process.env.DYRO_BROKER_TELEMETRY_PATH ?? "";
const providerMode = process.env.DYRO_PROVIDER_MODE ?? "fake";
const maxConcurrency = Math.max(
  1,
  Number(process.env.DYRO_BROKER_MAX_CONCURRENCY ?? "2"),
);
const rawRoot = process.env.DYRO_PROVIDER_RAW_ROOT ?? "/tmp/provider-raw";
const RAW_MARKER = "RAW_VENDOR_SECRET_MUST_NOT_ESCAPE";

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
    .replace(/sk-[A-Za-z0-9]+/g, "[REDACTED]")
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

function destroyRaw(callId: string): void {
  const path = join(rawRoot, `${callId}.raw`);
  if (existsSync(path)) {
    rmSync(path, { force: true });
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

async function runProvider(request: Request): Promise<{
  status: "ok" | "error" | "timeout";
  text: string;
  error_code: string;
  raw_destroyed: boolean;
}> {
  if (request.model !== allowedModel) {
    return {
      status: "error",
      text: "",
      error_code: "model_not_allowed",
      raw_destroyed: true,
    };
  }
  if (request.protocol_version !== 1 && request.protocol_version !== 2) {
    return {
      status: "error",
      text: "",
      error_code: "unsupported_protocol",
      raw_destroyed: true,
    };
  }
  if (request.protocol_version === 1 && request.schema_hint) {
    return {
      status: "error",
      text: "",
      error_code: "v1_rejects_schema_hint",
      raw_destroyed: true,
    };
  }

  if (providerMode === "fake") {
    const text = sanitize(
      `fake-provider:${request.model}:${request.prompt.trim().slice(0, 120)}`,
    );
    return { status: "ok", text, error_code: "", raw_destroyed: true };
  }

  if (providerMode !== "simulated-cli") {
    return {
      status: "error",
      text: "",
      error_code: "unknown_provider_mode",
      raw_destroyed: true,
    };
  }

  // simulated-cli: raw materialization only on Broker tmpfs.
  ensureRawRoot();
  const rawPath = join(rawRoot, `${request.call_id}.raw`);
  const rawBody = [
    "BEGIN PRIVATE KEY",
    RAW_MARKER,
    `prompt=${request.prompt}`,
    `model=${request.model}`,
    `schema_hint=${request.schema_hint ?? ""}`,
    "sk-simulated-vendor-token",
  ].join("\n");
  writeFileSync(rawPath, rawBody, { encoding: "utf8", mode: 0o600 });
  try {
    // Simulate a short CLI duration without leaving the isolation domain.
    await Bun.sleep(50);
    const sanitized = sanitize(
      `simulated-cli:${request.model}:${request.prompt.trim().slice(0, 80)}`,
    );
    if (sanitized.includes(RAW_MARKER) || sanitized.includes("BEGIN PRIVATE KEY")) {
      throw new Error("sanitizer failed to redacting raw markers");
    }
    return {
      status: "ok",
      text: sanitized,
      error_code: "",
      raw_destroyed: false,
    };
  } finally {
    destroyRaw(request.call_id);
  }
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
      rawDestroyed = result.raw_destroyed || !existsSync(join(rawRoot, `${request.call_id}.raw`));
      // Always force-destroy after response preparation.
      destroyRaw(request.call_id);
      rawDestroyed = !existsSync(join(rawRoot, `${request.call_id}.raw`));
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
        sanitizer: "stage2-v1",
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
  const line = `broker-ready ${host}:${port} mode=${providerMode} concurrency=${maxConcurrency}\n`;
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
