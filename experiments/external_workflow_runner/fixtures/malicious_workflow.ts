import { writeFileSync } from "node:fs";

function writeDenied(path: string): boolean {
  try {
    writeFileSync(path, "forbidden\n", "utf8");
    return false;
  } catch {
    return true;
  }
}

let networkDenied = false;
try {
  await fetch("https://example.com", {
    signal: AbortSignal.timeout(2_000),
  });
} catch {
  networkDenied = true;
}

let allowedWrite = false;
try {
  writeFileSync("/worktrees/docs/allowed.md", "allowed\n", "utf8");
  allowedWrite = true;
} catch {
  allowedWrite = false;
}

const probe = {
  allowed_write: allowedWrite,
  bundle_write_denied: writeDenied("/opt/workflow/forbidden.txt"),
  root_write_denied: writeDenied("/host-forbidden.txt"),
  network_denied: networkDenied,
  secret_absent: process.env.DYRO_EXECUTION_KEY === undefined,
};

writeFileSync(
  process.env.DYRO_RESULT_PATH ?? "/run/dyro/probe.json",
  `${JSON.stringify(probe)}\n`,
  "utf8",
);

if (!Object.values(probe).every(Boolean)) {
  process.exitCode = 2;
}
