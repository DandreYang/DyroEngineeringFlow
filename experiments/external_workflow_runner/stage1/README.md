# External Workflow Runner Stage 1

Stage 1 extends the Stage 0 isolation primitives with a frozen TypeScript
workflow runtime install, a fixed reviewed workflow bundle, an isolated Agent
Broker (fake provider), canonical input binding, claim renewal design, and a
hard gate that keeps execution keys out of Sandbox/Broker until cleanup is
verified.

This remains a removable experiment under `experiments/`. It is not part of the
installed `dyro` package and does not build, review, sign off, merge, or push
Dyro evidence.

## What Stage 1 proves

1. **Frozen runtime identity** — downloads the approved npm tarball only after
   matching `runtime-lock.json` integrity; writes a project-local frozen lock
   record with zero transitive production dependencies.
2. **Fixed bundle** — assembles `workflow.ts` + `broker_agent.ts` +
   `broker_server.ts` + vendored runtime sources; binds them with the Stage 0
   bundle manifest.
3. **Isolated Broker IPC** — Broker runs in its own container on a Docker
   `--internal` network, sharing a network namespace with the Workflow Sandbox
   so Agent calls use loopback TCP only (no external egress).
4. **Fake provider** — no credentials; responses are schema-checked and
   sanitized before telemetry is written.
5. **Canonical input** — RFC-style deterministic JSON bound to claim generation.
6. **Claim renewal** — Supervisor-only renewal before half-life; generation
   bump recorded.
7. **Execution key gate** — refuse to start if a key is already present;
   mount a simulated key only after Sandbox and Broker cleanup.

## Verification

```sh
docker pull oven/bun:1.3.11-slim
python -m unittest tests.test_external_workflow_runner_stage1
python -m unittest
```

Docker-backed Stage 1 tests place temporary bind-mount trees under the project
directory (Colima/virtiofs cannot mount host `/tmp` paths).

Results and remaining gaps: [`STAGE1_REPORT.md`](STAGE1_REPORT.md).

## Explicit non-goals

- real provider credentials or Codex/Claude CLIs
- Dyro evidence build / import / review / signoff / merge / push
- production multi-host isolation
- writable worktree storage quotas
