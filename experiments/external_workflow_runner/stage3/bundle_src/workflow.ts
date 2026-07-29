/**
 * Stage 3 multi-phase workflow:
 *   phase1 hold -> parallel agent branches via Broker -> phase3 hold
 * Claim renewals are handled by the Supervisor, not this process.
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
  return {
    host: process.env.DYRO_BROKER_HOST ?? "127.0.0.1",
    port: Number(process.env.DYRO_BROKER_PORT ?? "7421"),
  };
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
    deadline_ms: 8_000,
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
    }, 10_000);
    client.setEncoding("utf8");
    client.on("connect", () => client.write(`${JSON.stringify(request)}\n`));
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

async function main(): Promise<void> {
  const input = loadCanonicalInput();
  if (process.env.DYRO_WORKFLOW_RUN_ID !== input.workflow_run_id) {
    throw new Error("workflow_run_id mismatch");
  }

  const phase1 = Number(process.env.DYRO_STAGE3_PHASE1_MS ?? "1200");
  const phase3 = Number(process.env.DYRO_STAGE3_PHASE3_MS ?? "1200");

  // Phase 1: hold (claim renewal window).
  await Bun.sleep(phase1);

  // Phase 2: parallel agent branches through Broker argv-cli provider.
  const results = await parallel(
    input.branches.map(
      (branchId, index) => async () => {
        const summary = await callBroker(
          `Stage3 phase2 analyze ${branchId}`,
          input.model,
          `call-${index + 1}`,
        );
        return { id: branchId, status: "success" as const, summary };
      },
    ),
  );

  // Phase 3: hold again to allow another renewal cycle if needed.
  await Bun.sleep(phase3);

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
  const allCriticalOk = branches.every((b) => b.status === "success");

  const lines = [
    "# Stage 3 workflow report",
    "",
    `task: ${input.task_id}`,
    `runner: ${input.runner_id}`,
    `claim_generation_at_start: ${input.claim_generation}`,
    `provider_mode: ${process.env.DYRO_PROVIDER_MODE ?? "unknown"}`,
    `phases: hold=${phase1}ms, agents, hold=${phase3}ms`,
    "",
    ...branches.map(
      (item) =>
        `- ${item.id}: ${item.status} — ${item.summary.replace(/\n/g, " ")}`,
    ),
    "",
  ];
  const report = `${lines.join("\n")}`;
  writeFileSync(
    `/worktrees/${input.artifact_repository}/${input.artifact_path}`,
    report,
    "utf8",
  );
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
