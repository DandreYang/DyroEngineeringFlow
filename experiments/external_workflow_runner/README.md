# External Workflow Runner

This removable experiment tests the security assumptions for an optional
external TypeScript workflow runtime. It is not part of the installed `dyro`
package and does not change Dyro Core.

- Stage 0 isolation primitives: [`STAGE0_REPORT.md`](STAGE0_REPORT.md)
- Stage 1 frozen runtime + Broker IPC: [`stage1/STAGE1_REPORT.md`](stage1/STAGE1_REPORT.md)
- Stage 2 provider raw isolation + claim renewal: [`stage2/STAGE2_REPORT.md`](stage2/STAGE2_REPORT.md)
- Stage 3 argv-cli provider + claim matrix: [`stage3/STAGE3_REPORT.md`](stage3/STAGE3_REPORT.md)
- Stage 4 pinned provider + dual cleanup + evidence pack: [`stage4/STAGE4_REPORT.md`](stage4/STAGE4_REPORT.md)

## Current scope

Stage 0 implements and tests:

- a Docker Workflow Sandbox with a digest-pinned Bun image, no network,
  read-only root filesystem, no Linux capabilities, `no-new-privileges`,
  non-root execution, tmpfs, and CPU, memory, PID, output, and deadline limits;
- explicit environment allowlists for the Sandbox and Agent process primitive;
- construction-time snapshots and read-only views for trusted environment,
  worktree, manifest, branch, and artifact-policy inputs;
- deterministic bundle identity, file-set, size, and SHA-256 verification before
  and after every execution outcome, with bounded entry enumeration and a
  reserved manifest path;
- exact binding between the approved Docker image, non-root user, Bun version,
  and the checked-in runtime lock;
- a strict result envelope validator that rejects missing, duplicate, `null`,
  failed, or unknown critical branches;
- race-resistant artifact traversal using directory file descriptors and
  `O_NOFOLLOW`;
- a Broker concurrency/deadline primitive;
- bounded argv execution that kills the complete process group;
- a minimal Supervisor that validates the bundle, result, and artifacts and
  never receives a signing key.

The Docker runtime identity is fixed in
[`runtime-lock.json`](runtime-lock.json). The result shape is documented in
[`schemas/result-envelope.schema.json`](schemas/result-envelope.schema.json);
runtime validation additionally checks the expected branch set and
status-dependent invariants that JSON Schema alone cannot express.

## Verification

The integration tests require the pinned image:

```sh
docker pull oven/bun:1.3.11-slim
python -m unittest tests.test_external_workflow_runner_stage0
python -m unittest
```

The tests run malicious TypeScript at module top level and verify that:

- task-worktree writes succeed;
- bundle and root-filesystem writes fail;
- external network access fails;
- a host execution-key sentinel is absent;
- Workflow deadline cleanup removes the container;
- cleanup never removes a same-name container owned by another run and waits
  for a label-owned container that appears after Docker CLI termination;
- Agent-process timeout removes its full process group.

## Explicit gaps

Stage 1 still does not:

- call a real provider or mount provider credentials;
- build, sign, import, review, or sign off Dyro evidence;
- prove a production deployment or multi-host isolation boundary;
- make external event files part of Dyro-managed immutable evidence.

These gaps are deliberate. A signing key must not be introduced until the
Sandbox and Broker are gone and cleanup has been independently verified.
Passing Stage 1 means the frozen runtime install, fixed bundle, isolated
fake-provider Broker path, and execution-key gate are feasible locally.
