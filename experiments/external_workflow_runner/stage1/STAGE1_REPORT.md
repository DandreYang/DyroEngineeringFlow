# External Workflow Runner Stage 1 Report

- Date: 2026-07-29
- Branch: `task/external-workflow-runner-stage1`
- Source baseline: `5866ae9` (main after Stage 0 merge)
- Result: **GO for Stage 2 design** (local experiment only)
- Full PoC result: **Not yet evaluated**
- Production result: **Not ready**

## Environment

| Component | Identity |
| --- | --- |
| Host | macOS + Colima Docker (local); GitHub Actions ubuntu-latest (CI target) |
| Python | 3.14 |
| Bun image | `oven/bun@sha256:478281fdd196871c7e51ba6a820b7803a8ae97042ec86cdbc2e1c6b6626442d9` |
| Runtime package | `@dyro/semantic-flow@0.2.0` |
| Integrity | `sha512-sJgf79AHIwx67b570lMOuQjpouXepXSlfTeLXNobEubYzcViQZslnqRw2XEvYjF9+N3VUlpy6ID5qziSS1ICBw==` |
| Source tag / commit | `v0.2.0` / `73c61156197445be4a0fad390e3a1d802f2cda4a` |

## Stage 0 entry criteria coverage

| # | Requirement | Stage 1 evidence |
| --- | --- | --- |
| 1 | Frozen TS runtime + integrity | `install_verified_runtime()` verifies tarball sha512 against `runtime-lock.json`, vendors package, writes `runtime-package-lock.json` (`transitive_count: 0`) |
| 2 | Fixed reviewed bundle | `assemble_stage1_bundle()` copies `bundle_src/{workflow,broker_agent,broker_server}.ts` + vendor; manifest bind |
| 3 | Narrow Broker IPC in isolation | Docker internal network + shared netns; TCP JSON-line protocol schema in `protocol.py` / TS client |
| 4 | Fake provider before credentials | `broker_server.ts` returns sanitized fake text only |
| 5 | Canonical input + sanitized telemetry | `CanonicalInput` + `broker-telemetry.jsonl` with redaction |
| 6 | Claim renewal + no key until cleanup | `ClaimLease.renew`; Supervisor refuses pre-existing key; simulated mount only after cleanup |
| 7 | No Dyro evidence/signoff | Explicit non-call; tests assert no `.dyro` side effects |

## Commands and results

```text
python -m unittest tests.test_external_workflow_runner_stage1
Ran 10 tests in ~7s
OK

python -m unittest
OK (full suite, 148 test methods)

ruff check experiments tests/test_external_workflow_runner_stage0.py tests/test_external_workflow_runner_stage1.py
All checks passed

docker ps --all --filter name=dyro-s1
No residual Stage1 containers
```

## PoC mapping (delta from Stage 0)

| PoC ID | Stage 1 status | Notes |
| --- | --- | --- |
| POC-03 | Pass (local) | Exact package + integrity + source tag commit recorded |
| POC-04 | Partial | Package + vendor sources bound; full transitive lock is empty (package has no prod deps); wrappers/schema included in bundle manifest |
| POC-06 | Partial | Fake provider has no credentials; execution key gated; real provider still out of scope |
| POC-07 | Partial | Host unit tests exercise semaphore; container broker is single-threaded request handling on internal net |
| POC-17 | Partial | Fake provider never materializes vendor raw output; real CLI temp FS not yet present |
| POC-20 | Partial | Claim bind + renewal design tested; full control-plane claim file pipeline not wired |

## Defects closed in Stage 1

1. Colima cannot bind-mount host `/var/folders` or `/tmp` → Docker tests use project-local temp dirs.
2. AF_UNIX sockets do not work across Colima virtiofs host↔VM → switched to TCP loopback inside a Docker `--internal` network shared netns.
3. Long Unix socket paths exceeded macOS ~104 byte limit → irrelevant after TCP container broker.

## Explicit gaps (Stage 2+)

- Real provider adapter with credential isolation and raw-output temp FS destruction
- Broker concurrency semaphore inside the container path
- Claim lease covering gates + evidence import windows end-to-end
- Dyro evidence build only after independent cleanup verification (still forbidden in Stage 1)
- Worktree storage quotas
- Upstream package official test suite gate

## Stage 2 entry criteria (proposed)

1. Real fake-then-real provider adapter with no credentials in Sandbox
2. Prove raw provider output never leaves Broker temp FS
3. Claim renewal integrated with long-running workflows under deadline
4. Optional narrow IPC schema versioning and compatibility tests
5. Keep evidence/signoff out until 1–4 pass
