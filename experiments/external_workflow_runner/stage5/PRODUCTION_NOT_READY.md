# Production Not-ready checklist

This document is the operator-facing form of `production_gate.py`.
**Production must remain blocked** while any blocker below is open.

| ID | Requirement | Status | Blocks production? |
| --- | --- | --- | --- |
| PROD-01 | Multi-host / production container escape review | out_of_scope (local Docker only) | Yes |
| PROD-02 | Real Codex/Claude fleet binaries + credential mounts | partial (host pin path; fixture in CI) | Yes |
| PROD-03 | Dyro Core evidence import + review binding | fail (dry-run only) | Yes |
| PROD-04 | No third-party workflow package/brand | pass | No |
| PROD-05 | Sandbox never holds tokens/execution keys | pass | No |
| PROD-06 | Fail-closed critical branches | pass | No |
| PROD-07 | Dual cleanup before privileged post-run actions | pass | No |
| PROD-08 | Supervisor never merges/pushes | pass | No |
| PROD-09 | Worktree quotas on all writable mounts | partial (host worktree only) | Yes |
| PROD-10 | Replace TaskGraph with external runtime | out_of_scope (ADR veto) | No |

## ADR-0001 stop conditions (must remain true)

1. Must not modify Dyro scheduler/state machine solely to express internal phases.
2. Must isolate credentials and execution keys from the Sandbox.
3. Must fail-closed on critical branch failures.
4. Must not reintroduce third-party workflow package dependencies or brand traces.

## Verdict

**NOT_READY for production.** Local optional experiment may continue under Stage0–5 controls.
