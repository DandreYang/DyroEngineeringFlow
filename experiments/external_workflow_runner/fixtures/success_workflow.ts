import { createHash } from "node:crypto";
import { writeFileSync } from "node:fs";

const report = "# Stage 0 report\n";
writeFileSync("/worktrees/docs/report.md", report, "utf8");
const digest = createHash("sha256").update(report, "utf8").digest("hex");

const envelope = {
  schema_version: 1,
  status: "DONE",
  workflow_run_id: process.env.DYRO_WORKFLOW_RUN_ID,
  branches: [
    {
      id: "analysis-a",
      critical: true,
      status: "success",
      error_code: "",
    },
    {
      id: "analysis-b",
      critical: true,
      status: "success",
      error_code: "",
    },
  ],
  artifacts: [
    {
      repository: "docs",
      path: "report.md",
      sha256: digest,
    },
  ],
  question: "",
};

writeFileSync(
  process.env.DYRO_RESULT_PATH ?? "/run/dyro/result-envelope.json",
  `${JSON.stringify(envelope)}\n`,
  "utf8",
);
