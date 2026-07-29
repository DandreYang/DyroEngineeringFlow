/**
 * Fixed Stage 2 workflow: uses Stage 1-compatible Broker agent over TCP,
 * holds long enough for Supervisor claim renewal, and writes a result envelope.
 */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { createConnection } from "node:net";
import { parallel } from "./vendor/evaluated-typescript-runtime/src/flow/parallel.ts";

type CanonicalInput = {
  schema_version: 1;
  workflow_run_id: string;
  task_id: string;
  runner_id: string;
  claim_generation: number;
  branches: string[];
  artifact_repository: string;
  artifact_path: string;
  model: string;
  max_agent_calls: number;
};

function loadCanonicalInput(): CanonicalInput {
  const path = process.env.DYRO_CANONICAL_INPUT_PATH;
  if (!path) {
    throw new Error("DYRO_CANONICAL_INPUT_PATH is required");
  }
  return JSON.parse(readFileSync(path, "utf8")) as CanonicalInput;
}

function brokerEndpoint(): { host: string; port: number } {
  const host = process.env.DYRO_BROKER_HOST ?? "127.0.0.1";
  const port = Number(process.env.DYRO_BROKER_PORT ?? "7421");
  return { host, port };
}

function callBroker(prompt: string, model: string, callId: string): Promise<string> {
  const { host, port } = brokerEndpoint();
  const protocolVersion = Number(process.env.DYRO_IPC_PROTOCOL_VERSION ?? "2");
  const request: Record<string, unknown> = {
    protocol_version: protocolVersion,
    type: "agent.call",
    call_id: callId,
    prompt,
    model,
    cwd: "/worktrees/docs",
    deadline_ms: 5_000,
  };
  if (protocolVersion >= 2) {
    request.schema_hint = "text";
  }
  return new Promise((resolve, reject) => {
    const client = createConnection({ host, port });
    let buffer = "";
    const timer = setTimeout(() => {
      client.destroy();
      reject(new Error(`broker timeout ${callId}`));
    }, 6_000);
    client.setEncoding("utf8");
    client.on("connect", () => {
      client.write(`${JSON.stringify(request)}\n`);
    });
    client.on("data", (chunk: string) => {
      buffer += chunk;
      const nl = buffer.indexOf("\n");
      if (nl === -1) {
        return;
      }
      clearTimeout(timer);
      client.end();
      const payload = JSON.parse(buffer.slice(0, nl).trim()) as {
        status: string;
        text: string;
        error_code: string;
      };
      if (payload.status !== "ok") {
        reject(new Error(`broker ${payload.status}:${payload.error_code}`));
        return;
      }
      resolve(payload.text);
    });
    client.on("error", reject);
  });
}

async function analyzeBranch(
  branchId: string,
  model: string,
  index: number,
): Promise<{ id: string; status: "success"; summary: string }> {
  const summary = await callBroker(
    `Analyze branch ${branchId} for the Stage 2 documentation pilot.`,
    model,
    `call-${index + 1}`,
  );
  return { id: branchId, status: "success", summary };
}

async function main(): Promise<void> {
  const input = loadCanonicalInput();
  if (process.env.DYRO_WORKFLOW_RUN_ID !== input.workflow_run_id) {
    throw new Error("workflow_run_id mismatch");
  }

  // Hold long enough for Supervisor half-life claim renewal on short leases.
  const holdMs = Number(process.env.DYRO_STAGE2_HOLD_MS ?? "2500");
  await Bun.sleep(holdMs);

  const results = await parallel(
    input.branches.map(
      (branchId, index) => () => analyzeBranch(branchId, input.model, index),
    ),
  );

  const branches = input.branches.map((branchId, index) => {
    const item = results[index];
    if (!item) {
      return {
        id: branchId,
        critical: true,
        status: "failed" as const,
        error_code: "branch_null",
        summary: "",
      };
    }
    return {
      id: item.id,
      critical: true,
      status: "success" as const,
      error_code: "",
      summary: item.summary,
    };
  });
  const allCriticalOk = branches.every((branch) => branch.status === "success");

  const lines = [
    "# Stage 2 workflow report",
    "",
    `task: ${input.task_id}`,
    `runner: ${input.runner_id}`,
    `claim_generation_at_start: ${input.claim_generation}`,
    `provider_mode: ${process.env.DYRO_PROVIDER_MODE ?? "unknown"}`,
    "",
    ...branches.map(
      (item) =>
        `- ${item.id}: ${item.status} — ${item.summary.replace(/\n/g, " ")}`,
    ),
    "",
  ];
  const report = `${lines.join("\n")}`;
  const artifactPath = `/worktrees/${input.artifact_repository}/${input.artifact_path}`;
  writeFileSync(artifactPath, report, "utf8");
  const digest = createHash("sha256").update(report, "utf8").digest("hex");

  const envelope = {
    schema_version: 1,
    status: allCriticalOk ? "DONE" : "BLOCKED",
    workflow_run_id: input.workflow_run_id,
    branches: branches.map(({ id, critical, status, error_code }) => ({
      id,
      critical,
      status,
      error_code,
    })),
    artifacts: allCriticalOk
      ? [
          {
            repository: input.artifact_repository,
            path: input.artifact_path,
            sha256: digest,
          },
        ]
      : [],
    question: "",
  };

  writeFileSync(
    process.env.DYRO_RESULT_PATH ?? "/run/dyro/result-envelope.json",
    `${JSON.stringify(envelope)}\n`,
    "utf8",
  );
}

await main();
