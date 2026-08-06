# Dyro Agent Bridge Phase 0 Construction Blueprint

Status: Proposed

Objective: deliver a source-audited, installable, real-host-verified inbound
inspect-and-plan interface for coding agents without exposing Dyro mutation.

Authority:

- [ADR 0006](../docs/adr/0006-agent-bridge-phase-0.md)
- [Operation inventory](../docs/designs/agent-bridge-operation-inventory.md)
- [Protocol](../docs/designs/agent-bridge-protocol.md)
- [Acceptance matrix](../docs/designs/agent-bridge-phase-0-acceptance.md)
- [Adversarial review board](../docs/superpowers/reviews/2026-08-06-dyro-agent-bridge-design-adversarial-review-board.md)
- [Phase 0 design closure review](../docs/superpowers/reviews/2026-08-06-dyro-agent-bridge-phase-0-design-closure-review.md)

This plan does not authorize commit, push, PR, merge, tag, release, publish, or
integration installation. Those remain separate user decisions.

## 1. Fixed invariants

Every implementation step preserves these invariants:

1. Dyro Core remains the only delivery-policy and mutation authority.
2. Phase 0 contains no Agent apply operation or generic command execution.
3. Bridge code does not call CLI `cmd_*` handlers or parse human CLI output.
4. R0 is zero-write, zero-network, and zero unexpected subprocess.
5. PLAN output is deterministic, non-executable, and unauthorised.
6. Local Profile discovery fails closed and cannot fall back to a different
   workspace when a local Profile exists but is invalid.
7. A host is unsupported until its real discovery and sandbox evidence passes.
8. Existing unrelated worktree changes are preserved and never broadly staged.

## 2. Dependency graph

```text
S1 Core contracts and Exposure Catalog
 ├─> S2 Typed workspace resolution and observations
 └─> S3 Typed deterministic plan services
       S2 + S3 ─> S4 One-shot JSON transport
       S4 ─> S5 Zero-effect and artifact gates
       S5 ─> S6 Host-neutral Skill beta
       S5 + S6 ─> S7 Codex typed read-only MCP/Plugin
```

S2 and S3 may proceed in parallel after S1 because they own separate Core
modules and tests. S1 must first freeze `WorkspaceIdentityV1` and
`ConfigRevisionV1`; neither parallel step may invent its own identity. All
other steps are serial gates.

## 3. Step S1 — Core contracts and Exposure Catalog

Implementation status: Complete on 2026-08-06 for the source-tree unit gates
only. All operations remain deny-by-default and unavailable; this does not
satisfy the S5 public-availability, installed-artifact, or release gates.

### Context brief

The catalog is exposure metadata, not a second policy engine. The protocol
needs immutable request/response metadata, stable risk types, schema versions,
and an allowlist before any transport exists.

### Ownership

- `src/dyro/bridge/__init__.py`
- `src/dyro/bridge/models.py`
- `src/dyro/bridge/catalog.py`
- `src/dyro/bridge/schemas.py`
- `pyproject.toml` package declaration and core JSON Schema validator dependency
- `uv.lock`
- `tests/fixtures/bridge/contracts-v1.json`
- `tests/test_bridge_models.py`
- `tests/test_bridge_catalog.py`

### Tasks

1. Define frozen protocol, operation, risk, availability, error, warning, and
   response models without importing CLI.
2. Implement a deny-by-default catalog containing only Phase 0 declared IDs;
   keep them unavailable until their service proof is registered.
3. Generate compact capabilities and per-operation schema separately.
4. Canonicalize and hash the compact catalog.
5. Define and vector-test `WorkspaceIdentityV1` and `ConfigRevisionV1`, including
   move/rename semantics, domain separators, canonical path handling, Profile
   file bounds, and the fact that neither value authenticates a caller.
6. Implement availability states `declared`, `implemented_testable`, and
   `public_available`, plus the non-empty Mandatory Core Surface assertion.
7. Add import guards proving bridge Core modules do not import `dyro.cli`.

### Verification

```bash
uv run python -m unittest tests.test_bridge_models tests.test_bridge_catalog -v
uv run ruff check src/dyro/bridge tests/test_bridge_models.py tests/test_bridge_catalog.py
```

### Exit criteria

- The A01 catalog/schema **unit portion**, A02 schema portion, A03, and D03 unit
  gates pass. Formal A01 public-availability and artifact assertions are made
  only at S5 after the transport and mandatory services exist.
- Catalog contains no apply, run, gate execution, sign-off, merge, push,
  release, publish, or cleanup operation.
- The catalog's release-mode validator fails on a fixture that omits a mandatory
  operation or declares an empty public surface. Development catalogs may still
  contain only `declared`/`implemented_testable` operations before S5.

### Rollback

Remove the new isolated bridge package and tests. No existing CLI/Core behavior
should require rollback.

## 4. Step S2 — Typed workspace resolution and observations

Implementation status (2026-08-06): source-tree Core work is complete and the
four S2 services are `implemented_testable`. They remain unavailable to public
Bridge callers until the S4 transport and S5 zero-effect/artifact gates pass.

### Context brief

Reuse `continuation.resolution` precedence and `observations.py` composition,
but do not expose exceptions, paths, internal dataclasses, or recovery-enabled
Objective reads. R0 must survive read-only state roots without creating paths.

### Ownership

- `src/dyro/read_limits.py`
- focused additions to `src/dyro/config.py`
- focused additions to `src/dyro/hub.py`
- focused additions to `src/dyro/tasks.py`
- focused additions to `src/dyro/workspace.py`
- focused additions to `src/dyro/continuation/objective_storage.py`
- focused additions to `src/dyro/continuation/store.py`
- `src/dyro/bridge/observations.py`
- focused additions to `src/dyro/continuation/resolution.py`
- `tests/test_bridge_resolution.py`
- `tests/test_bridge_observations.py`

### Tasks

1. Add transport-neutral `ObservationLimits` / `ReadBudget` primitives that
   open safe regular files, enforce per-file and aggregate byte budgets, and
   stop at an injected monotonic deadline without creating state.
2. Add `load_profile_exact(root, budget) -> LoadedProfile`; registry roots must
   contain their own bounded `dyro.toml` and must never search a parent Profile.
   The same bounded bytes feed parsing and `ConfigRevisionV1`.
3. Add `resolve_workspace_readonly(...) -> ResolvedWorkspace` with typed source
   and typed failure reason; it is non-interactive, never expands `~`, never
   updates recent state and never parses human-facing exception text.
4. Return typed resolution source and stable error codes while retaining current
   explicit/local/default/unique precedence.
5. Add DTO allowlists for workspace, line, task, graph, Objective and gate
   definition observations.
6. Make every Objective observation use `recover=False` explicitly.
7. Separate Git observation behind a documented optional-lock-disabled adapter;
   do not add it to an operation until B05 passes.
8. Add stat-before-read file limits, per-class record caps, aggregate-byte
   budget, deadline and per-record fault isolation. One malformed Task or
   Objective cannot erase healthy siblings.
9. Distinguish `integration_inspection=complete|not_inspected|partial`. Summary
   DTOs omit final readiness; authoritative explain/status remains unavailable
   until the Git adapter passes B05.
10. Inject clock and limits so tests prove deterministic bounded results.

### Verification

```bash
uv run python -m unittest tests.test_bridge_resolution tests.test_bridge_observations tests.test_console_read_model tests.test_continuation_resolution -v
uv run ruff check src/dyro/bridge/observations.py src/dyro/observations.py src/dyro/continuation/resolution.py tests/test_bridge_resolution.py tests/test_bridge_observations.py
```

### Exit criteria

- C01, C02 unit portion, and the no-recovery unit gate pass.
- Malformed local Profile never produces a registry workspace result.
- `task.gate_definitions.get` cannot reach `run_gates` or subprocess APIs.
- Oversized or excessive workspace input becomes a bounded per-record/partial
  result, never a request-wide silent empty list.

### Rollback

Remove Bridge DTO adapters. Keep only independently useful Core fixes that have
their own tests and do not alter human CLI semantics.

## 5. Step S3 — Typed deterministic plans

### Context brief

Existing Objective plan/tick/attention paths are mostly pure, but Bridge plans
need explicit schema and planner revisions, typed operation-specific read sets,
and language that cannot be mistaken for authorization.

### Ownership

- `src/dyro/bridge/plans.py`
- focused Core plan payload additions under `src/dyro/continuation/`
- `tests/test_bridge_plans.py`
- canonical vectors under `tests/fixtures/bridge/`

### Tasks

1. Define typed read sets separately for Objective plan, explain, graph, tick,
   and attention operations.
2. Add `executable=false`, `authorization=none`, schema version and planner
   revision to every Bridge plan.
3. Define an operation-specific typed `projection` that preserves selected,
   blocked, graph, attention, and tick-wave results separately from effects.
4. Allowlist, bound, and deterministically redact the final plan payload, then
   compute RFC 8785 `plan_sha256` over that transport-safe payload excluding
   only the digest itself.
5. Prove identical facts/input/clock produce identical plans; any safety fact,
   redacted visible value, or planner revision change produces a new digest.
6. Do not implement a plan consumer or confirmation/apply model.

### Verification

```bash
uv run python -m unittest tests.test_bridge_plans tests.test_continuation_supervision tests.test_continuation_attention -v
uv run ruff check src/dyro/bridge/plans.py tests/test_bridge_plans.py
```

### Exit criteria

- D05 passes for every plan operation.
- Search confirms no Bridge plan is consumed by a mutation path.

### Rollback

Remove Bridge plan adapters and schema vectors; existing continuation planning
remains intact.

## 6. Step S4 — One-shot JSON transport

### Context brief

The machine boundary must own parsing and errors before human argparse and
terminal rendering. It reads one bounded request and writes one bounded response
while stdout remains writable; a broken pipe follows the explicit exit-5 rule.

### Ownership

- `src/dyro/bridge/transport.py`
- `src/dyro/bridge/redaction.py`
- `pyproject.toml` entry point and optional transport dependencies
- `MANIFEST.in` only if non-Python schemas are packaged
- `tests/test_bridge_transport.py`
- `tests/test_bridge_redaction.py`

### Tasks

1. Implement strict duplicate-key-aware bounded JSON parsing.
2. Validate envelope then one operation schema before resolving a workspace.
3. Route only to catalog-bound Core services.
4. Normalize all failures into stable redacted errors and enforce output limits.
5. Define parse-stage error metadata with nullable unknown request fields and
   separate server/requested protocol values.
6. Handle broken stdout as deterministic exit 5 without retry or traceback;
   exactly-one-JSON applies only while stdout is writable.
7. Add the real `dyro-bridge` console entry point.
8. Keep MCP dependencies and host integration files out of this step.

### Verification

```bash
uv run python -m unittest tests.test_bridge_transport tests.test_bridge_redaction -v
uv run ruff check src/dyro/bridge tests/test_bridge_transport.py tests/test_bridge_redaction.py
uv run python -m compileall -q src tests
```

### Exit criteria

- D01–D05 pass in source-tree tests.
- Human CLI output and error behavior are unchanged.
- No transport request shape contains mutation or approval fields.

### Rollback

Remove the console entry point and isolated transport package. Core Observation
and Plan services remain available to the Console/CLI if independently useful.

## 7. Step S5 — Zero-effect, artifact, and real-sandbox gates

### Context brief

The original failure mode was visible only in a real restricted Codex
environment. This step must prove behavior beyond mocks and writable temporary
state roots.

### Ownership

- `tools/verify_bridge_zero_effects.py`
- `tests/test_bridge_black_box.py`
- `.github/workflows/ci.yml`
- artifact test fixtures and protocol corpus

### Tasks

1. Audit file creation/write-open and relevant metadata across HOME, XDG,
   `DYRO_HOME`, workspace, Git metadata and temp roots.
2. Deny network and record process starts; allow only reviewed Git read argv.
3. Inject pending Objective recovery, stale registry, permission failures and
   malformed local Profiles.
4. Run one protocol corpus against source, wheel, and sdist outside checkout.
5. Perform real Codex in-sandbox and out-of-sandbox trials and preserve evidence.
6. Run E03-Core protocol/schema/planner current/N-1 fixtures and incompatible
   future/unknown cases. Do not require MCP or an integration artifact here.
7. Use the platform-specific layered observation mechanisms from the acceptance
   matrix and record their blind spots; unsupported platform operations fail
   closed.

### Verification

```bash
uv run python -m unittest tests.test_bridge_black_box -v
uv run python tools/verify_bridge_zero_effects.py
uv run python -m build -o /tmp/dyro-bridge-dist
```

Artifact install commands must use a newly created temporary directory and
environment; exact commands are recorded with the evidence rather than assumed
by this plan.

### Exit criteria

- A01–E02 and E03-Core pass on current commit, and every Mandatory Core Surface
  operation is publicly callable in source, wheel, and sdist.
- Phase 0 Core + JSON transport may move from Conditional Go to Go.
- Any write, recovery, network, secret leak, or unexpected process is a hard
  stop, not a warning.

### Rollback

Disable all catalog availability and remove the entry point if a zero-effect
property cannot be proven. Keep the harness as a regression tool.

## 8. Step S6 — Host-neutral Skill beta

### Context brief

The Skill is progressive-disclosure guidance over a proven Phase 0 transport.
It is not an authorization or security boundary and must not be confused with
outbound multi-Agent `dispatch`.

### Ownership

- host-neutral Skill source under a new integration source directory
- integration ownership manifest
- Skill trigger and context-budget tests

### Tasks

1. Write a Skill that first calls compact capabilities, then fetches one schema.
2. Define positive Dyro inspect/plan triggers and negative dispatch/mutation
   triggers.
3. Implement previewed install/status/uninstall with conflict detection and a
   recoverable ownership manifest.
4. Run the ten fresh-session journeys and measure byte/token budgets.

### Verification

Use the exact host discovery directory and fresh sessions. A copied Skill file
that was not actually discovered is not acceptance evidence.

### Exit criteria

- F01, F03, and F04 pass.
- Skill beta is explicitly Codex-only until another host passes independently.

### Rollback

Uninstall only files owned by the manifest and restore any atomically retained
prior version. Never delete an unowned same-name Skill.

## 9. Step S7 — Codex typed read-only MCP/Plugin

### Context brief

MCP adapts the same Core services and exposes a small typed tool set. Server
code ships with Dyro; the Codex integration artifact versions discovery and
compatibility independently.

### Ownership

- `src/dyro/bridge/mcp.py`
- `dyro-mcp` entry point and optional dependency metadata
- Codex integration artifact and compatibility manifest
- MCP process, version-skew, install/update/uninstall/rollback tests

### Tasks

1. Map only the approved typed read/plan operations.
2. Start the installed `dyro-mcp` executable, never ambient `python -m`.
3. Handshake Core/integration/protocol/schema/planner/capabilities versions and
   fail closed on incompatible combinations.
4. Prove the MCP process obeys the actual Codex permission boundary.
5. Verify install, update, failure rollback, status, and uninstall ownership.
6. Complete E03-Integration: Core-newer, integration-newer, N/N-1, missing MCP
   dependency, and tool-list pinning without exposure widening.

### Verification

Run all Phase 0 protocol and zero-effect gates through MCP, plus F02. Inspect
the actual advertised tool list and assert mutation names are absent.

### Exit criteria

- All Core gates, E03-Integration, and F01–F04 pass through the installed Codex
  integration.
- Public wording names only the verified host and read/plan scope.

### Rollback

Disable or uninstall the integration artifact without removing Core Bridge.
Protocol incompatibility fails closed and leaves the previous owned artifact
recoverable.

## 10. Adversarial review gates

An independent reviewer must challenge the implementation after S4 and again
after S7. The reviewer receives the locked branch/HEAD, relevant diff, ADR,
inventory, protocol, acceptance matrix, test evidence, wheel/sdist, and real
host evidence.

The review tries to prove:

- an excluded operation is reachable;
- an R0 path writes, recovers, networks, or starts an unknown process;
- a local malformed Profile falls back to another workspace;
- a digest is presented as authorization;
- CLI handler/presentation logic leaked into Core Bridge;
- raw paths, secrets, argv, logs, or exceptions cross the transport;
- packaging or version skew widens the tool surface;
- a host claim is based on source-tree or mocked behavior rather than a real
  installed session.

Any confirmed P0/P1 finding returns the affected step to in-progress. The
reviewer cannot approve mutation as part of Phase 0.

## 11. Plan mutation protocol

- Split a step when it no longer has one independently verifiable outcome.
- Insert a prerequisite before dependent work; never mark the dependent step
  complete with a waived invariant.
- Reorder only when the dependency graph and file ownership remain valid.
- Record skipped work with the exact reason and resulting unsupported claim.
- Abandon a step by disabling its catalog availability and removing owned
  integration files; preserve evidence and unrelated work.
- Any proposed apply, R1/R2/R3 operation, broker, daemon, or approval token is a
  new project with a new ADR and review, not a mutation of this blueprint.
