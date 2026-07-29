# External Workflow Runner Stage 0 Report

- Date: 2026-07-29
- Branch: `task/external-workflow-runner-stage0`
- Source baseline: `7d5fe1bbe05a`
- Result: **GO for Stage 1 local experimentation**
- PoC result: **Not yet evaluated**
- Production result: **Not ready**

## Environment

| Component     | Fixed or observed identity                                                         |
| ------------- | ---------------------------------------------------------------------------------- |
| Host          | macOS Darwin 25.6.0, x86_64                                                        |
| Python        | 3.14.3                                                                             |
| Docker server | 29.2.1                                                                             |
| Docker client | 29.3.1                                                                             |
| Ruff          | 0.15.2                                                                             |
| Bun image     | `oven/bun@sha256:478281fdd196871c7e51ba6a820b7803a8ae97042ec86cdbc2e1c6b6626442d9` |
| Bun runtime   | 1.3.11, image user `1000:1000`                                                     |

The host `sandbox-exec` binary exists, but even an allow-all profile failed with
`sandbox_apply: Operation not permitted` in this execution environment. The
native Seatbelt path was therefore rejected for this Stage 0 run. Docker is the
only tested isolation backend.

## Verified behavior

The Stage 0 test module exercises:

- deterministic bundle file-set, size, identity, and SHA-256 binding;
- rejection of bundle symlinks, undeclared files, reserved manifest paths,
  excessive entry enumeration, and post-build changes;
- exact binding of the checked-in runtime lock to the Docker image, non-root
  user, and Bun version used by the Sandbox;
- exact result-envelope fields, run ID, expected branch set, status invariants,
  artifact descriptors, and malicious JSON types;
- directory-FD and `O_NOFOLLOW` artifact access, including final and parent
  symlink rejection, traversal rejection, size limits, and hash verification;
- Broker semaphore enforcement and queue-inclusive per-call deadlines;
- rejection of zero, boolean, non-finite, and otherwise invalid deadline or CPU
  limits;
- explicit child environment, bounded stdout/stderr, and complete process-group
  termination;
- construction-time snapshots and read-only views that prevent callers from
  widening environment, worktree, branch, manifest, or artifact policies after
  validation;
- a child that ignores `SIGTERM`, which is removed with `SIGKILL` after the
  grace window;
- digest-pinned Docker execution with no network, a read-only root filesystem,
  all capabilities dropped, `no-new-privileges`, non-root user, tmpfs, and
  CPU, memory, PID, output, and total deadlines;
- malicious TypeScript running at module top level, not only inside an Agent
  adapter;
- Supervisor execution that re-verifies the bundle, validates a unique result
  envelope, securely reopens the declared artifact, and verifies container
  cleanup before returning, including non-zero and validation-failure paths;
- ownership-labelled Docker cleanup that refuses same-name foreign containers
  and waits through the bounded daemon late-creation window.

Five runtime-specific defects were found and closed during the spike:

1. Bun did not expose container environment variables under arbitrary UID
   `65532`; the pinned image's built-in non-root identity `1000:1000` is now
   part of the runtime lock.
2. On macOS, signalling an already-exited process-group ID can return `EPERM`;
   the runner now preserves fail-closed status, waits the direct process, and
   still proves that a live stubborn descendant is killed.
3. A caller could mutate trusted mappings and sets after frozen dataclass
   validation; those inputs are now copied and exposed through read-only views,
   while the Supervisor retains private defensive snapshots.
4. Explicit zero and non-finite deadlines could bypass positive-number checks;
   all deadline, grace, and CPU inputs now require finite values.
5. Docker daemon work can outlive a killed CLI and create the container after
   the first cleanup check; cleanup now waits through a bounded settle window,
   removes only matching ownership labels, and never deletes a foreign
   same-name container.

## Commands and results

```text
/Users/dandre/HYCProjects/DyroEngineeringFlow/.venv/bin/python -m unittest
Ran 138 tests in 31.012s
OK

/Users/dandre/HYCProjects/DyroEngineeringFlow/.venv/bin/ruff check --no-cache experiments tests/test_external_workflow_runner_stage0.py
All checks passed!

/Users/dandre/HYCProjects/DyroEngineeringFlow/.venv/bin/ruff format --no-cache --check experiments tests/test_external_workflow_runner_stage0.py
12 files already formatted

npx --yes --cache /private/tmp/external-runner-npm-cache prettier@3.6.2 --check README.md STAGE0_REPORT.md runtime-lock.json result-envelope.schema.json fixtures/*.ts
All matched files use Prettier code style!

docker ps --all --filter name=dyro-stage0
No matching containers
```

## Priority acceptance mapping

| PoC ID | Stage 0 status | Evidence and remaining gap                                                                                                                                          |
| ------ | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| POC-04 | Partial        | Manifest binds the complete test bundle, approved image, user, and Bun identity. The selected third-party runtime and frozen transitive lock are not installed yet. |
| POC-05 | Pass           | A real top-level TypeScript attack is confined by the local Docker backend. Multi-host and container escape review remain out of scope.                             |
| POC-06 | Partial        | Sandbox and child environments are explicit and omit the execution-key sentinel. No real Broker or execution-key mount exists yet.                                  |
| POC-07 | Partial        | The semaphore and queue-inclusive deadline pass concurrency tests. They are not yet exposed through Broker IPC or a provider adapter.                               |
| POC-08 | Pass           | Timeout removes a complete process group, including a child that ignores `SIGTERM`.                                                                                 |
| POC-09 | Pass           | Workflow deadline removes only the label-owned Docker container, waits through late daemon creation, and verifies that no container remains.                        |
| POC-11 | Pass           | Artifact allowlist, canonical path, directory-FD traversal, symlink rejection, type, size, and SHA-256 checks pass.                                                 |
| POC-24 | Partial        | Memory, CPU, PID, tmpfs, result, bundle, artifact, and output limits exist. Writable task worktrees have no storage quota yet.                                      |

A `Pass` here applies only to the local Stage 0 implementation and tests. It is
not equivalent to passing the full PoC acceptance item in a real provider run.

## Stage 1 entry criteria

Stage 1 may proceed locally because no Stage 0 test produced an isolation
escape, false `DONE`, orphan process, or residual container. Stage 1 must:

1. install the selected TypeScript workflow runtime using a project-local
   frozen lock and verify the recorded package integrity;
2. execute a fixed reviewed workflow bundle rather than the direct TypeScript
   fixture;
3. define and test a narrow Broker IPC protocol in a separately isolated
   process or container;
4. route a fake provider through that IPC before introducing real credentials;
5. bind canonical input and sanitized Workflow/Broker telemetry;
6. design claim renewal and prove that no execution key exists until Sandbox
   and Broker cleanup has been verified;
7. keep Dyro evidence build, review, signoff, merge, and push out of Stage 1
   until these controls pass.
