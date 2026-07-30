# External Workflow Runner Stage 5 Report (PoC closeout)

- Date: 2026-07-29
- Branch: `task/external-workflow-runner-stage5-closeout`
- Source baseline: `d0d9d5f` (main after Stage 4 merge)
- Result: **Local experiment path CLOSED**
- Full PoC result: **Local isolation proven; productization not authorized**
- Production result: **Not ready** (hard gate)
- 2026-07-30 follow-on: Production Candidate operator UX and signed Core handoff added; environment authorization remains blocked

## Stage 4 entry criteria coverage

| # | Requirement | Stage 5 evidence |
| --- | --- | --- |
| 1 | Host-mounted provider + path allowlist + content pin; Sandbox has no network to host secrets | `HostProviderPin` + Broker-only RO bind; Sandbox uses internal netns only |
| 2 | Sealed pack verification and runner-side Core handoff | `dry_run_validate_pack` + `core_handoff.py`; runtime never imports |
| 3 | Production Not-ready checklist vs ADR-0001 | `production_gate.py` + `PRODUCTION_NOT_READY.md`; tests assert NOT_READY |

## Commands and results

```text
python3 -m unittest tests.test_external_workflow_runner_stage5
Ran 9 tests … OK
```

## Explicit non-goals retained

- No Dyro Core dependency on Bun / semantic-flow
- No signoff / merge / push from Supervisor
- No evidence import from the runtime; only a signed Core-compatible bundle is built
- No multi-host / container-escape certification

## Follow-on (only if productization is explicitly authorized)

1. Operator-managed real provider binary inventory + pin rotation
2. ~~Control-plane evidence handoff with independent review binding~~ (implemented 2026-07-30)
3. Multi-host isolation and volume-quota certification
