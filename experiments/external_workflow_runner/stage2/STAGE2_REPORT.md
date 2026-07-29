# External Workflow Runner Stage 2 Report

- Date: 2026-07-29
- Branch: `task/external-workflow-runner-stage2`
- Source baseline: `09c79bb` (main after Stage 1 merge)
- Result: **GO for Stage 3 design** (local experiment only)
- Full PoC result: **Not yet evaluated**
- Production result: **Not ready**

## Stage 1 entry criteria coverage

| # | Requirement | Stage 2 evidence |
| --- | --- | --- |
| 1 | Provider adapter without Sandbox credentials | `DYRO_PROVIDER_MODE=fake\|simulated-cli` only in Broker container |
| 2 | Raw provider output never leaves Broker temp FS | `simulated-cli` writes `/tmp/provider-raw/<id>.raw`, destroys before reply; marker absent from telemetry/artifacts |
| 3 | Claim renewal during long workflows | `ClaimRenewalLoop` while workflow holds ≥2.5s; generation increments |
| 4 | IPC protocol versioning | v1/v2 accepted; v3 rejected; v1 rejects `schema_hint` |
| 5 | No evidence/signoff | Explicit non-call; tests assert no `.dyro` |

## Commands and results

```text
python -m unittest tests.test_external_workflow_runner_stage2
Ran 5 tests … OK

python -m unittest tests.test_external_workflow_runner_stage0 tests.test_external_workflow_runner_stage1 tests.test_external_workflow_runner_stage2
OK

ruff check experiments/external_workflow_runner/stage2 tests/test_external_workflow_runner_stage2.py
All checks passed
```

## PoC mapping (delta)

| PoC ID | Stage 2 status | Notes |
| --- | --- | --- |
| POC-06 | Improved | Still no real credentials; simulated-cli proves raw isolation path |
| POC-07 | Improved | In-broker semaphore + `max_observed_concurrency` telemetry |
| POC-17 | Pass (local simulated) | Raw on tmpfs only; destroyed before response; leak tests |
| POC-20 | Improved | Mid-run Supervisor renewal with generation bump |

## Explicit gaps (Stage 3+)

- Real Codex/Claude CLI provider with credential mounts only in Broker
- Full claim lease covering gates + evidence import windows
- Dyro evidence build after cleanup (still forbidden)
- Worktree storage quotas
- Upstream package official test suite gate

## Stage 3 entry criteria (proposed)

1. Real provider CLI adapter with minimal credentials in Broker only
2. Prove raw CLI stdout/stderr never leave Broker tmpfs after call end
3. Wire claim file through a longer multi-phase deadline matrix
4. Keep evidence/signoff out until 1–3 pass
