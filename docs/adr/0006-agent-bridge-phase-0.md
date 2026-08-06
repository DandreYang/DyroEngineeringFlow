# ADR 0006: Agent Bridge Phase 0

## Status

Proposed

## Context

Dyro already exposes a human CLI, a read-only local Console, an outbound and
advisory `dispatch` experiment, and a native continuation engine. Coding agents
still lack a stable inbound contract for discovering a workspace, reading
delivery state, explaining blockers, and producing a deterministic plan. They
therefore fall back to human-formatted CLI output or direct filesystem access,
both of which are brittle and easy to misinterpret.

An earlier Agent Bridge proposal combined read operations, planning, generic
confirmed apply, Skill delivery, MCP tools, and cross-host Plugin packaging in
one v1. The adversarial review at
[`2026-08-06-dyro-agent-bridge-design-adversarial-review-board.md`](../superpowers/reviews/2026-08-06-dyro-agent-bridge-design-adversarial-review-board.md)
rejected that scope. The subsequent
[Phase 0 design closure review](../superpowers/reviews/2026-08-06-dyro-agent-bridge-phase-0-design-closure-review.md)
closed eight implementation-blueprint findings and approved only the S1
contract/catalog step. Current source disproves two key assumptions:

- `task gates` executes configured argv, writes gate logs, and appends the
  ledger; it is not a read operation.
- a confirmation SHA can bind a plan to observed facts, but the same Agent can
  read and replay it; it is not proof of an independent human approval.

The repository does contain a useful starting point:
`capture_workspace_read_snapshot()` already composes lines, tasks, and
Objectives with `recover=False` for Objective reads. The Bridge should harden
and reuse Core-owned observations rather than parse or call CLI commands.

## Decision

Dyro will introduce **Agent Bridge Phase 0** as an inbound, machine-readable,
inspect-and-plan-only surface.

1. Phase 0 exposes no operation that mutates workspace, global registry, Git,
   Task, Objective, evidence, audit, configuration, preferences, cache, or host
   integration state.
2. Phase 0 exposes no generic shell, arbitrary command, `apply`, sign-off,
   merge, push, release, publish, cleanup, Agent launch, gate execution,
   recovery, repair, or update check.
3. A Core-owned **Observation service** produces immutable typed facts without
   parsing human CLI output and without calling a CLI `cmd_*` handler.
4. A Core-owned **Plan service** produces deterministic, explicitly
   non-executable plans. A plan may describe effects that a human could later
   request through the existing CLI, but the Bridge cannot execute them.
5. The former Operation Registry is replaced by an **Exposure Catalog**. The
   catalog contains exposure metadata only: operation ID, schemas, maximum
   risk, protocol compatibility, availability, and a reference to a Core
   service. Authorization, policy, locks, transactions, and business
   invariants remain in Core.
6. A dedicated `dyro-bridge` one-shot transport will accept one bounded JSON
   request on stdin and, while stdout remains writable, emit exactly one
   bounded JSON response on stdout. A broken output pipe exits deterministically
   without retry or traceback. Routing, validation, and error rendering do not
   enter the human argparse or terminal-decoration path.
7. CLI transport may use one schema-validated, allowlisted operation field for
   inspect and plan. A later MCP adapter must expose a small set of typed tools;
   it may not publish generic execute or generic apply tools.
8. Workspace resolution reuses the existing precedence: explicit alias,
   upward local Profile discovery, registered default, then a unique usable
   registered workspace. A malformed local Profile fails closed and never
   falls back to a different workspace.
9. Compact capabilities return IDs, risk, availability, and schema versions.
   A client fetches the full schema for one selected operation on demand.
10. Core Bridge and MCP server code ship in the Dyro Python distribution, with
    real `dyro-bridge` and later `dyro-mcp` console entry points. A host-specific
    Plugin, manifest, or installed Skill is a separately versioned integration
    artifact with an explicit compatibility range and reversible installation.
11. Codex is the first candidate host. No other host is described as supported
    before its install, discovery, process, sandbox, approval, upgrade, and
    rollback journeys pass end-to-end tests.
12. Opening any mutation to an Agent requires a separate ADR and adversarial
    review for one typed operation. Phase 0 approval cannot be reused as
    mutation approval.

## Authority and threat model

Dyro Core remains the sole delivery control plane. A Skill is guidance, an MCP
tool list is an exposure boundary, and a plan digest is an integrity check;
none of them is an authorization credential.

Phase 0 protects against accidental misuse by exposing only observations and
non-executable plans. It does not claim to isolate a malicious coding agent
that already has the same operating-system identity, shell access, and direct
permission to invoke the human `dyro` CLI. A stronger claim such as “the Agent
cannot sign off, merge, or push” would require every mutation entry point to
pass through a common broker or externally signed policy boundary that the
model cannot forge or read.

If a future host supplies approval, the host capability must be model-invisible,
short-lived, single-use, and bound to the operation, canonical input, plan
digest, workspace identity, effects, host session, expiry, and a random nonce.
Until that property is proven in a real host, no Agent apply operation exists.

## Risk vocabulary

The Exposure Catalog uses the maximum possible authority of an operation:

| Class | Meaning | Phase 0 |
| --- | --- | --- |
| `R0` | Pure observation; no persistent write, network, or non-allowlisted subprocess | Allowed after proof |
| `PLAN` | Pure deterministic plan; explicitly non-executable | Allowed after proof |
| `R1` | Recoverable control-plane write | Not exposed |
| `R2` | Agent, gate, Git-worktree, evidence, or other execution write | Not exposed |
| `R3` | Sign-off, merge, push, release, publish, destructive cleanup, or credential authority | Not exposed |

Risk is deny-by-default. A command name, a `dry_run` flag, or an intended new
implementation is not evidence of R0. Each catalog entry requires a source
call graph and negative tests that fail on writes, network, and unexpected
process creation.

## Observation invariants

Every R0 operation must satisfy all of the following:

1. no persistent semantic write and no newly created file, directory, lock,
   cache, temp artifact, preference, or recent-state record;
2. no recovery, repair, update check, hydration mutation, or lazy index write;
3. no network access and no Agent or configured gate launch;
4. Git reads use optional locks disabled and are tested on every supported
   platform for index and metadata stability;
5. partial failures are explicit and bounded; one bad component does not erase
   healthy components or cause fallback to another workspace;
6. raw exception text, argv, stdout/stderr, remote URL, environment value,
   absolute path, prompt, answer, or gate log is not returned by default;
7. all fields pass an explicit DTO allowlist, size limit, and secret-redaction
   boundary before transport serialization;
8. workspace input is bounded before parsing: readers stat regular files before
   use, cap per-file and aggregate bytes, cap enumerated records, enforce a
   deadline, and isolate a malformed record without erasing healthy siblings;
9. any result derived without authoritative Git integration inspection reports
   `integration_inspection: "not_inspected"` and cannot claim final readiness,
   dispatchability, or integration blocking.

## Workspace identity and configuration revision

Phase 0 does not assume a persisted workspace UUID that current Core does not
have. S1 freezes two explicit local identifiers:

- `WorkspaceIdentityV1` is
  `SHA-256("dyro.workspace.identity/v1\0" + JCS({"canonical_root":
  <resolved-profile-root>, "profile_name": <validated-name>}))`. The response
  exposes only `workspace:<hex>`. It is stable while canonical root and Profile
  name are unchanged and deliberately changes after moving or renaming the
  workspace. It is not an identity credential.
- `ConfigRevisionV1` is
  `SHA-256("dyro.config.raw/v1\0" + <exact dyro.toml bytes>)`, computed only
  after the Profile is proven to be a bounded safe regular file. Comments or
  formatting changes invalidate the revision by design, avoiding an incomplete
  semantic-field allowlist in Phase 0.

Neither payload is returned. The domain separators, canonical path semantics,
Profile byte limit, and test vectors are part of the S1 contract, so S2
observations and S3 plans can be implemented independently without inventing
different identities.

## Plan invariants

A Phase 0 plan is data, not authority. Its envelope contains at least:

- `executable: false` and `authorization: "none"`;
- protocol major, operation ID, operation schema version, and planner revision;
- canonical workspace identity and configuration digest;
- normalized input;
- an operation-specific `read_set` containing every predicate and resolved
  object used by planning;
- an operation-specific typed `projection`, semantic effects, warnings,
  maximum/effective risk, and expiry;
- a canonical `plan_sha256` that detects drift but is never named or treated as
  user approval.

The digest is computed over the final transport-safe, allowlisted and redacted
canonical plan payload, excluding only `plan_sha256`. A client therefore sees
every hashed field. Raw Core objects or pre-redaction content never contribute
hidden digest material.

No generic read-set schema is assumed to cover every operation. A future apply
would have to acquire the domain's authoritative lock, replan under that lock,
compare the digest, and use operation-specific idempotency, linearization,
durable intent/start/receipt, fencing, uncertainty, and recovery semantics.

## Workspace resolution contract

The response reports `resolution_source` as one of `explicit`, `local`,
`default`, or `unique`. It distinguishes at least:

- `LOCAL_PROFILE_INVALID`;
- `REGISTRY_INVALID`;
- `WORKSPACE_NOT_REGISTERED`;
- `REGISTERED_ROOT_STALE`;
- `HOST_READ_PERMISSION_REQUIRED`;
- `AMBIGUOUS_WORKSPACE`;
- `WORKSPACE_NOT_FOUND`.

Each error may include bounded structured `next_actions`, but never a shell
string for automatic execution. Resolution does not mark a workspace recent.

## Distribution and compatibility

- The Python distribution owns Core semantics and the Bridge/MCP server code.
- The host integration artifact owns host discovery metadata, Skill content,
  installation ownership, and compatibility declarations.
- The handshake includes Core, Bridge, integration, protocol, operation-schema,
  planner, and capabilities-digest versions.
- Unknown protocol major, unknown operation, unavailable dependency, or an
  incompatible schema range fails closed.
- Adding a Core operation does not automatically expose it through an older
  Plugin or existing MCP session.
- Wheel and sdist acceptance runs outside the source checkout and verifies
  installed entry points and packaged schemas.

## Platform scope

Phase 0 targets Linux Ubuntu 24.04 and macOS 15 for Core/JSON acceptance. Codex
host integration is initially targeted on macOS 15. Windows receives only an
import and fail-closed discovery smoke in Phase 0 because current Objective
storage does not provide the same directory-fd guarantees there; Objective and
other unsupported operations report `OPERATION_UNAVAILABLE`.

Availability is per operation and per platform. Linux acceptance combines
in-process denial traps with `strace` file/network/process evidence and Git
metadata snapshots. macOS acceptance combines in-process traps, read-only
roots, before/after metadata snapshots, process shims, and the real managed
Codex sandbox; any remaining OS-observer blind spot is recorded as `须人工核`
and prevents a public support claim. One evidence layer cannot substitute for
another.

## Consequences

Coding agents gain a reliable way to understand Dyro and explain a safe next
step without scraping terminal text. Phase 0 also creates a transport-neutral
Core boundary that the CLI, Console, and future MCP adapter can share.

The cost is that the first release intentionally cannot complete a user action.
It also requires explicit DTOs, schema/version governance, side-effect tracing,
and real-host black-box tests before a Skill or Plugin can be called supported.

## Non-goals

- Replacing the human CLI.
- Turning `dispatch` into an inbound control interface.
- Executing a plan, gate, Agent, Git mutation, recovery, or repair.
- Treating a digest, `--yes`, `actor`, request ID, or Skill instruction as user
  authorization.
- Shipping one universal transaction or idempotency layer over current
  line/task/workspace mutations.
- Claiming cross-host support based only on locating or launching a host CLI.
- Exposing raw configuration, paths, prompts, logs, environment, or credentials.

## Acceptance criteria

- The approved operation inventory contains only source-audited R0 and PLAN
  entries; `task.gates`, `task.answer`, mutation, and delivery operations are
  absent.
- Bridge imports Core services and never imports or calls CLI `cmd_*` handlers.
- Every success and failure produces exactly one bounded JSON object on stdout
  while stdout is writable, with no ANSI, traceback, or secret-bearing raw
  exception. A broken pipe produces no retry and no traceback.
- All R0 operations pass deny-write, deny-network, and deny-unexpected-process
  tests with read-only HOME, `DYRO_HOME`, workspace, and temp roots.
- Pending Objective transactions are observed without recovery or mutation.
- Workspace resolution preserves local Profile precedence and fail-closed
  behavior with stable reason codes and structured next actions.
- Source-tree, wheel, and sdist installations produce compatible schemas and
  results outside the checkout.
- The non-empty Mandatory Core Surface is public-available in each supported
  artifact; a catalog with zero available operations cannot pass.
- Codex is not marked supported until actual discovery and sandbox journeys pass.
- Phase 0 schemas contain no apply operation and mark every plan
  `executable: false`, `authorization: "none"`.
