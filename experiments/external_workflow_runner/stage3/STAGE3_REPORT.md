# External Workflow Runner Stage 3 Report

- Date: 2026-07-29
- Branch: `task/external-workflow-runner-stage3`
- Source baseline: `408d8ad` (main after Stage 2 merge)
- Result: **GO for Stage 4 design** (local experiment only)
- Full PoC result: **Not yet evaluated**
- Production result: **Not ready**

## Stage 2 entry criteria coverage

| # | Requirement | Stage 3 evidence |
| --- | --- | --- |
| 1 | Real provider CLI adapter; credentials only in Broker | `provider_mode=argv-cli` + `Bun.spawn` fixed argv; `DYRO_PROVIDER_FAKE_TOKEN` only in Broker env |
| 2 | Raw CLI stdout/stderr never leave Broker tmpfs | Capture to `/tmp/provider-raw/*.stdout|stderr`, destroy before reply; leak tests |
| 3 | Claim multi-phase deadline matrix | `ClaimDeadlineMatrix` + phase1/phase2/phase3 workflow holds + mid-run renewal |
| 4 | No evidence/signoff | Explicit non-call; no `.dyro` side effects |

## Commands and results

```text
python -m unittest tests.test_external_workflow_runner_stage3
Ran 4 tests … OK
```

## PoC mapping (delta)

| PoC ID | Stage 3 status | Notes |
| --- | --- | --- |
| POC-06 | Improved | Provider token only in Broker; sandbox env deny-list for token/key |
| POC-08 | Pass (fixture CLI) | argv spawn + full process wait; no shell concatenation |
| POC-17 | Pass (fixture CLI) | stdout/stderr on tmpfs only; destroyed; markers absent downstream |
| POC-20 | Improved | Matrix-sized lease + mid-run renewal across multi-phase workflow |

## Explicit gaps (Stage 4+)

- Real Codex/Claude binaries and credential mounts (still fixture CLI)
- Dyro evidence build after cleanup verification
- Worktree storage quotas
- Upstream package official test suite gate

## Stage 4 entry criteria (proposed)

1. Optional real provider binary behind allowlisted argv + integrity pin
2. Evidence packing only after dual cleanup verification (sandbox + broker)
3. Keep merge/push out of Supervisor
