# External Semantic Runtime PoC — Final Evaluation

- Date: 2026-07-29
- Scope: `experiments/external_workflow_runner` Stage0–5
- Runtime: first-party `@dyro/semantic-flow` (no third-party workflow package)

## Executive verdict

| Question | Answer |
| --- | --- |
| Can Dyro run a fixed TS semantic workflow behind isolation locally? | **Yes** |
| Can credentials and execution keys stay out of the Sandbox? | **Yes** (fixture/host pin paths) |
| Can results be sealed after dual cleanup without merge/push? | **Yes** |
| Is production deployment authorized? | **No — NOT_READY** |

## Stage trail

| Stage | Focus | Outcome |
| --- | --- | --- |
| 0 | Docker sandbox isolation primitives | GO Stage1 |
| 1 | Frozen runtime + Broker IPC + claim gate | GO Stage2 |
| 2 | Provider raw isolation + claim renewal | GO Stage3 |
| 3 | argv-cli + claim matrix | GO Stage4 |
| 4 | Provider pin + dual cleanup + local evidence pack | GO Stage5 |
| 5 | Host provider + dry-run + production gate | **Local path closed** |

## Priority acceptance mapping (rollup)

| ID | Verdict | Notes |
| --- | --- | --- |
| POC-04 | Pass (first-party) | content-hash locked `ts_runtime` |
| POC-05 | Pass (local) | malicious TS confined by Docker |
| POC-06 | Pass (local) | tokens/keys not in Sandbox |
| POC-07 | Pass (local) | broker semaphore + deadlines |
| POC-08 | Pass | process group + argv CLI wait |
| POC-09 | Pass | label-owned container cleanup |
| POC-11 | Pass | artifact FD path + pack hashes |
| POC-17 | Pass (fixture/host) | raw tmpfs destroy |
| POC-20 | Pass | claim matrix + mid-run renewals |
| POC-24 | Partial | host worktree quota only |
| Evidence import | Dry-run only | no Core import |
| Production | **NOT_READY** | see PRODUCTION_NOT_READY.md |

## What this PoC deliberately did not prove

- Multi-host isolation / container escape resistance in production orchestrators
- Real vendor CLI fleets and enterprise credential vaults
- Dyro evidence import, independent review binding, signoff, merge, push
- Performance/capacity under sustained multi-tenant load

## Recommendation

Keep the experiment **optional and removable**. Do **not** promote into Dyro Core.
Any productization requires an explicit new decision that clears every production blocker.
