/**
 * Stage 1 Agent Broker (fake provider) — TCP JSON-line protocol.
 * Runs in an isolated container that shares a network namespace with the
 * Workflow Sandbox. No provider credentials are loaded.
 */
import { appendFileSync } from "node:fs";
import { createServer } from "node:net";

const port = Number(process.env.DYRO_BROKER_PORT ?? "7421");
const host = process.env.DYRO_BROKER_HOST ?? "127.0.0.1";
const allowedModel = process.env.DYRO_BROKER_MODEL ?? "fake-model";
const telemetryPath = process.env.DYRO_BROKER_TELEMETRY_PATH ?? "";

type Request = {
  protocol_version: 1;
  type: "agent.call";
  call_id: string;
  prompt: string;
  model: string;
  cwd: string;
  deadline_ms: number;
};

function sanitize(text: string): string {
  return text
    .replaceAll("BEGIN PRIVATE KEY", "[REDACTED]")
    .replaceAll("execution-key", "[REDACTED]")
    .replaceAll("DYRO_EXECUTION_KEY", "[REDACTED]")
    .replace(/sk-[A-Za-z0-9]+/g, "[REDACTED]")
    .slice(0, 512);
}

function appendTelemetry(event: Record<string, unknown>): void {
  if (!telemetryPath) {
    return;
  }
  const line = `${JSON.stringify(event)}\n`;
  appendFileSync(telemetryPath, line, { encoding: "utf8", mode: 0o600 });
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
    const started = Date.now();
    let response: Record<string, unknown>;
    try {
      const request = JSON.parse(line) as Request;
      if (
        request.protocol_version !== 1 ||
        request.type !== "agent.call" ||
        !request.call_id ||
        !request.prompt
      ) {
        throw new Error("invalid_request");
      }
      if (request.model !== allowedModel) {
        response = {
          protocol_version: 1,
          type: "agent.result",
          call_id: request.call_id,
          status: "error",
          text: "",
          error_code: "model_not_allowed",
        };
      } else {
        response = {
          protocol_version: 1,
          type: "agent.result",
          call_id: request.call_id,
          status: "ok",
          text: sanitize(
            `fake-provider:${request.model}:${request.prompt.trim().slice(0, 120)}`,
          ),
          error_code: "",
        };
      }
      appendTelemetry({
        schema_version: 1,
        kind: "agent_call",
        call_id: request.call_id,
        model: request.model,
        status: response.status,
        error_code: response.error_code,
        duration_ms: Date.now() - started,
        prompt_chars: request.prompt.length,
        response_chars: String(response.text ?? "").length,
        sanitizer: "stage1-v1",
      });
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
  });
});

server.listen(port, host, () => {
  process.stdout.write(`broker-ready ${host}:${port}\n`);
});

function shutdown(): void {
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 500).unref();
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
