# External Workflow Runner

This first-party experiment tests the security assumptions for an optional
external TypeScript workflow runtime. Python modules ship with the `dyro`
wheel:

- `import experiments.external_workflow_runner…`
- `dyro runtime status|doctor|plan|claim|handoff|production-acceptance|production-gate`
- `python -m experiments.external_workflow_runner production-gate`

It does not change Dyro Core delivery authority. The runtime is a Production
Candidate but remains `NOT_READY` until the environment blockers clear.

- Stage 0 isolation primitives: [`STAGE0_REPORT.md`](STAGE0_REPORT.md)
- Stage 1 frozen runtime + Broker IPC: [`stage1/STAGE1_REPORT.md`](stage1/STAGE1_REPORT.md)
- Stage 2 provider raw isolation + claim renewal: [`stage2/STAGE2_REPORT.md`](stage2/STAGE2_REPORT.md)
- Stage 3 argv-cli provider + claim matrix: [`stage3/STAGE3_REPORT.md`](stage3/STAGE3_REPORT.md)
- Stage 4 pinned provider + dual cleanup + evidence pack: [`stage4/STAGE4_REPORT.md`](stage4/STAGE4_REPORT.md)
- Stage 5 host provider + dry-run + PoC closeout: [`stage5/STAGE5_REPORT.md`](stage5/STAGE5_REPORT.md) · [`stage5/POC_EVALUATION.md`](stage5/POC_EVALUATION.md)

## Current scope

Stage 0 implements and tests:

- a Docker Workflow Sandbox with a digest-pinned Bun image, no network,
  read-only root filesystem, no Linux capabilities, `no-new-privileges`,
  non-root execution, tmpfs, and CPU, memory, PID, output, and deadline limits;
- explicit environment allowlists for the Sandbox and Agent process primitive;
- construction-time snapshots and read-only views for trusted environment,
  worktree, manifest, branch, and artifact-policy inputs;
- deterministic bundle identity, file-set, size, and SHA-256 verification before
  and after every execution outcome, with bounded entry enumeration and a
  reserved manifest path;
- exact binding between the approved Docker image, non-root user, Bun version,
  and the checked-in runtime lock;
- a strict result envelope validator that rejects missing, duplicate, `null`,
  failed, or unknown critical branches;
- race-resistant artifact traversal using directory file descriptors and
  `O_NOFOLLOW`;
- a Broker concurrency/deadline primitive;
- bounded argv execution that kills the complete process group;
- a minimal Supervisor that validates the bundle, result, and artifacts and
  never receives a signing key.

The Docker runtime identity is fixed in
[`runtime-lock.json`](runtime-lock.json). The result shape is documented in
[`schemas/result-envelope.schema.json`](schemas/result-envelope.schema.json);
runtime validation additionally checks the expected branch set and
status-dependent invariants that JSON Schema alone cannot express.

Production environment acceptance uses
[`schemas/production-deployment-manifest.schema.json`](schemas/production-deployment-manifest.schema.json)
and
[`schemas/production-attestation.schema.json`](schemas/production-attestation.schema.json).
The gate requires a release signature plus purpose-separated security,
provider, and quota signatures from four distinct trusted public keys. Missing
evidence leaves the corresponding blocker open; signed evidence never grants
runtime delivery authority.

`dyro runtime production-acceptance` closes the operator usability gap without
moving authority into the runtime: it locates packaged schemas, hashes real
release/evidence files, prepares create-only unsigned records, exports
domain-separated RFC 8785 bytes for an external signer/HSM, and verifies the
returned signature before create-only attachment. It has no production private
key or deployment command. The complete procedure is in the
[production acceptance operator runbook](../../docs/production-acceptance-operator-runbook.md).

## Verification

The integration tests require the pinned image:

```sh
docker pull oven/bun:1.3.11-slim
python -m unittest tests.test_external_workflow_runner_stage0
python -m unittest
```

The tests run malicious TypeScript at module top level and verify that:

- task-worktree writes succeed;
- bundle and root-filesystem writes fail;
- external network access fails;
- a host execution-key sentinel is absent;
- Workflow deadline cleanup removes the container;
- cleanup never removes a same-name container owned by another run and waits
  for a label-owned container that appears after Docker CLI termination;
- Agent-process timeout removes its full process group.

## Production-candidate boundary

The shipped runtime still does not:

- prove real provider credentials and fleet recovery;
- import, review, sign off, merge, or push Dyro evidence;
- prove a production deployment or multi-host isolation boundary;
- enforce storage quotas at every production writable mount.

Those statements describe the current repository/environment evidence. Once
the real environment supplies the four release-bound signatures, operators can
verify them without editing code:

```sh
dyro runtime production-gate \
  --root /control/dyro-profile \
  --release-manifest /release/manifest.json \
  --security-attestation /evidence/prod-01.json \
  --provider-attestation /evidence/prod-02.json \
  --quota-attestation /evidence/prod-09.json
```

`READY` still requires independent release approval and does not deploy,
import, review, sign off, merge, or push.

After Sandbox and Broker cleanup is independently verified, the trusted
runner-side handoff may use an execution key to build a signed Core-compatible
bundle. It still cannot import that bundle. See
[`docs/designs/external-runtime-production-readiness.md`](../../docs/designs/external-runtime-production-readiness.md).
