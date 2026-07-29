/**
 * Fixed, reviewed Stage 1 workflow bundle.
 *
 * Uses the vendored evaluated TypeScript workflow runtime for control-flow
 * helpers when available, and routes every Agent call through the Broker IPC
 * adapter. This module never reads execution keys or provider credentials.
 */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { parallel } from "./vendor/evaluated-typescript-runtime/src/flow/parallel.ts";
import { BrokerAgent, bindAgent } from "./broker_agent.ts";

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
  const payload = JSON.parse(readFileSync(path, "utf8")) as CanonicalInput;
  if (
    payload.schema_version !== 1 ||
    !payload.workflow_run_id ||
    !Array.isArray(payload.branches) ||
    payload.branches.length < 1
  ) {
    throw new Error("canonical input failed validation");
  }
  return payload;
}

const agent = bindAgent(new BrokerAgent());

async function analyzeBranch(
  branchId: string,
  model: string,
): Promise<{ id: string; status: "success"; summary: string }> {
  const summary = await agent(
    `Analyze branch ${branchId} for the Stage 1 documentation pilot.`,
    {
      model,
      cwd: "/worktrees/docs",
    },
  );
  return {
    id: branchId,
    status: "success",
    summary: String(summary),
  };
}

async function main(): Promise<void> {
  const input = loadCanonicalInput();
  if (process.env.DYRO_WORKFLOW_RUN_ID !== input.workflow_run_id) {
    throw new Error("workflow_run_id mismatch");
  }

  const results = await parallel(
    input.branches.map(
      (branchId) => () => analyzeBranch(branchId, input.model),
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
    "# Stage 1 workflow report",
    "",
    `task: ${input.task_id}`,
    `runner: ${input.runner_id}`,
    `claim_generation: ${input.claim_generation}`,
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
