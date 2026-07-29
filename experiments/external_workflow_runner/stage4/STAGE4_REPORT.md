# External Workflow Runner Stage 4 Report

- Date: 2026-07-29
- Branch: `task/external-workflow-runner-stage4`
- Source baseline: `66f3721` (main after first-party semantic-flow + history purge)
- Result: **GO for Stage 5 design** (local experiment only)
- Full PoC result: **Not yet evaluated**
- Production result: **Not ready**

## Stage 3 entry criteria coverage

| # | Requirement | Stage 4 evidence |
| --- | --- | --- |
| 1 | Optional real provider binary behind allowlisted argv + integrity pin | `ProviderBinaryPin` + Broker `DYRO_PROVIDER_ARGV_SHA256` verify before ready |
| 2 | Evidence packing only after dual cleanup verification | `CleanupProof` + `pack_run_evidence` after sandbox+broker containers absent |
| 3 | Keep merge/push out of Supervisor | `refuse_if_merge_requested`; pack `non_goals` include no_signoff/merge/push |

## Additional Stage 4 controls

- Worktree storage quota (`WorktreeQuota`) fail-closed after run
- Provider token still Broker-only; raw markers still destroyed on Broker tmpfs
- Claim multi-phase matrix + mid-run renewal retained from Stage 3

## Commands and results

```text
python3 -m unittest tests.test_external_workflow_runner_stage4
Ran 9 tests … OK

python3 -m unittest \
  tests.test_external_workflow_runner_stage0 \
  tests.test_external_workflow_runner_stage1 \
  tests.test_external_workflow_runner_stage2 \
  tests.test_external_workflow_runner_stage3 \
  tests.test_external_workflow_runner_stage4
Ran 64 tests … OK
```

No residual `dyro-s4*` containers after the suite.

## PoC mapping (delta)

| PoC ID | Stage 4 status | Notes |
| --- | --- | --- |
| POC-06 | Pass (fixture) | Token only in Broker; pin path is bundle-local fixture |
| POC-08 | Pass (fixture) | argv-only spawn + content SHA pin |
| POC-11 | Pass | Artifacts sealed into local pack after cleanup |
| POC-17 | Pass (fixture) | Raw on tmpfs; destroy; dual cleanup gate before pack |
| POC-20 | Pass | Claim matrix + mid-run renewals retained |
| Evidence pack | Local only | Not Dyro Core import; no signoff/merge/push |

## Explicit gaps (Stage 5+)

- Real Codex/Claude host binaries (path + integrity pin still fixture-oriented)
- Production Dyro evidence import / review binding
- Multi-host isolation proof
- Storage quotas under Docker volume drivers beyond host worktree bytes

## Stage 5 entry criteria (proposed)

1. Optional host-mounted real provider binary with allowlisted path + content pin + no network from Sandbox
2. Optional import of Stage 4 pack into a **non-production** Dyro evidence dry-run validator (still no merge/push)
3. Document production Not-ready checklist against ADR-0001 stop conditions
