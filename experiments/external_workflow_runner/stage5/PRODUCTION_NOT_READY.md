# Production Not-ready checklist（2026-07-30）

This document is the operator-facing form of `production_gate.py`.
**Production must remain blocked** while any blocker below is open.

| ID | Requirement | Status | Blocks production? |
| --- | --- | --- | --- |
| PROD-01 | Multi-host / production container escape review | out_of_scope (local Docker only) | Yes |
| PROD-02 | Real Codex/Claude fleet binaries + credential mounts | partial (host pin path; fixture in CI) | Yes |
| PROD-03 | Stage5-to-Core execution evidence handoff | pass (signed bundle + independent review binding E2E) | No (cleared) |
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

**NOT_READY for production.** Open blockers are `PROD-01`, `PROD-02`, and
`PROD-09`. The optional runtime is a **Production Candidate**, not an authorized
production deployment.

Use the executable gate rather than reading this snapshot:

```sh
dyro runtime production-gate
# NOT_READY => exit 3

dyro runtime production-acceptance schemas --human
# first actionable step for collecting signed production acceptance
```

## How real evidence can close the blockers

This snapshot remains `NOT_READY`; it is not a permanent hard-coded dead end.
After the real release environment completes the three reviews, the gate can
consume:

1. a `production-release` signed deployment manifest;
2. a `production-security` signed `PROD-01` attestation;
3. a `production-provider` signed `PROD-02` attestation;
4. a `production-quota` signed `PROD-09` attestation.

All records bind the same release manifest and environment. The four roles
must use distinct trusted public keys. Attestations expire within 31 days and
bind durable evidence URIs plus SHA-256 values. The executable contract rejects
tampering, weak pass assertions, revoked keys, role mismatch, expiry, and
cross-release drift. See
[`docs/designs/external-runtime-production-readiness.md`](../../../docs/designs/external-runtime-production-readiness.md).
The create-only file hashing and external HSM flow is documented in the
[`production acceptance operator runbook`](../../../docs/production-acceptance-operator-runbook.md).

The gate only reports readiness. A `READY` result still requires independent
release approval and never performs deployment or Dyro Core delivery actions.
