# External Workflow Runner Stage 5（PoC closeout + Production Candidate）

Stage 5 closes the **local** experiment path:

1. **Host-mounted provider** — allowlisted absolute host path + content SHA pin; bind-mounted **Broker-only**
2. **Evidence verification** — validate Stage4-style sealed pack, cross-file identity, cleanup and workspace artifacts
3. **Core handoff** — cap Stage5 leases by an exported Core claim and build a signed Core execution bundle after cleanup
4. **Production gate** — return exit code 3 while environment blockers remain
5. **Signed acceptance** — verify one release manifest and three independently
   signed, expiring environment attestations without granting deployment authority

The handoff only builds a bundle. It never imports evidence or performs
review/signoff/merge/push. See
[`docs/designs/external-runtime-production-readiness.md`](../../../docs/designs/external-runtime-production-readiness.md).

Operator entry points:

```sh
dyro runtime status
dyro runtime doctor
dyro runtime plan
dyro runtime claim prepare --help
dyro runtime handoff --help
dyro runtime production-gate  # exit 3 while NOT_READY
dyro runtime production-gate --help  # signed acceptance inputs
```

Core claim and the execution signing key must both be private regular files
(`0600` on POSIX), never symlinks. The signing key must live outside the Dyro
Profile, runner workspace, and Stage5 pack; dry-run enforces the same preflight.

The production gate accepts `--release-manifest`, `--security-attestation`,
`--provider-attestation`, and `--quota-attestation`. See the packaged schemas
under `../schemas/` and the
[production-readiness design](../../../docs/designs/external-runtime-production-readiness.md).
The current repository still has no real-environment evidence, so the
zero-input gate remains `NOT_READY`.

## Verification

```sh
docker pull oven/bun:1.3.11-slim
python3 -m unittest tests.test_external_workflow_runner_stage5
python3 -m unittest tests.test_runtime_core_handoff_integration
python3 -m unittest tests.test_production_acceptance
python3 -m unittest
```

See:

- [`STAGE5_REPORT.md`](STAGE5_REPORT.md)
- [`POC_EVALUATION.md`](POC_EVALUATION.md)
- [`PRODUCTION_NOT_READY.md`](PRODUCTION_NOT_READY.md)
