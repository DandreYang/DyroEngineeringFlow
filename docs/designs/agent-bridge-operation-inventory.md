# Dyro Agent Bridge Operation Inventory

Status: `0.7.x` source catalog may mark the seven Mandatory IDs
`public_available` on Linux only. Published wheel/sdist do not include
`dyro.bridge` or `dyro-bridge`, so that promotion is not an installed
product surface.

Decision source: [ADR 0007](../adr/0007-agent-bridge-phase-0.md)

Review outcomes are incorporated into ADR 0007 and the Phase 0 acceptance
matrix; point-in-time review exports are not part of the product documentation.

## 1. Purpose

This inventory prevents operation names from being mistaken for authority or
side-effect evidence. Every Agent-facing operation starts unavailable and is
enabled only after its real Core call graph and negative side-effect tests are
recorded. CLI handlers are evidence about current behavior, not reusable
Bridge services.

Status vocabulary:

- `declared`: schema and catalog metadata exist, but no service is callable;
- `implemented_testable`: a service is callable only through an internal test
  harness; the public transport still returns `OPERATION_UNAVAILABLE`;
- `public_available`: the installed transport exposes the operation after its
  unit, zero-effect, protocol, and artifact gates pass;
- `deferred`: potentially useful, but outside the first slice;
- `excluded`: prohibited from Phase 0;
- `future-review`: a mutation candidate requiring a separate ADR and review.

Risk vocabulary follows ADR 0007: `R0`, `PLAN`, `R1`, `R2`, and `R3`.

## 2. Phase 0 declared surface

| Operation | Class | Status | Current source starting point | Required Core work and proof |
| --- | --- | --- | --- | --- |
| `bridge.hello` | R0 | public_available (Linux) | `bridge/transport.py` | Return only protocol/Core/Bridge version; no update or host probing |
| `bridge.capabilities.compact` | R0 | public_available (Linux) | `bridge/catalog.py`, `bridge/transport.py` | IDs, risk, availability, versions and digest only; no full schemas |
| `bridge.operation.schema` | R0 | public_available (Linux) | `bridge/schemas.py`, `bridge/transport.py` | Fetch exactly one allowlisted callable schema; reject unknown/unavailable operation |
| `workspace.resolve` | R0 | public_available (Linux) | `bridge/observations.py`, `continuation/resolution.py` | Typed result with `resolution_source`; structured fail-closed errors; no recent-state write |
| `workspace.list` | R0 | public_available (Linux) | `bridge/observations.py`, `hub.py` | DTO without absolute paths by default; partial stale/unreadable status; no registry mutation |
| `workspace.observe` | R0 | public_available (Linux) | `bridge/observations.py` | Bounded per-record partial results; mark integration `not_inspected`; never infer final readiness |
| `line.list` | R0 | declared | `workspace.py:117` | Typed projection; no CLI formatting; path fields excluded |
| `task.list` | R0 | declared | `tasks.py:212` | Summary only unless Git inspection is complete; no final dispatchability from status text |
| `task.explain` | R0 | declared | `graph.py`, scheduler snapshot | Authoritative explanation requires reviewed Git-read adapter and B05; otherwise unavailable |
| `task.graph` | R0 | declared | `graph.py`, `cli.py:1754` | Typed nodes/edges/issues; validation cannot repair state or run gates |
| `task.gate_definitions.get` | R0 | implemented_testable | `bridge/observations.py`, bounded Task loader | Return gate names and redacted metadata only; must never call `run_gates` |
| `objective.list` | R0 | declared | `continuation/store.py:425` | New wrapper must call `list_objectives(..., recover=False)` |
| `objective.status` | R0 | declared | scheduler snapshot | Final ready/blocked result requires reviewed Git inspection; summary reports `not_inspected` |
| `objective.plan` | PLAN | public_available (Linux) | `bridge/plans.py`, pure continuation planner | Typed projection/read set, bounded metadata-validated Git inspection, planner revision, non-executable Bridge digest |
| `objective.explain` | PLAN | implemented_testable | `bridge/plans.py`, pure continuation planner | Code-only summary/reasons; incomplete integration fails closed |
| `objective.graph` | PLAN | implemented_testable | `bridge/plans.py`, scheduler projection | Opaque typed nodes/edges only; no mutation or recovery |
| `objective.tick` | PLAN | implemented_testable | `bridge/plans.py`, pure scheduler tick | Typed wave/deferrals/non-mutating actions; no lease, intent, reservation, or execution |
| `objective.attention` | PLAN | implemented_testable | `bridge/plans.py`, pure attention projection | Typed priority/kind/reason/action-kind; never writes presentation state |

The S3 Git boundary ignores caller `PATH`, uses only validated system Git and
system Python binder executables, disables lazy fetch and replace objects,
rejects config includes and object alternates, requires Git metadata to remain
inside the workspace, and caps one plan at 100 Git process starts. The Linux
implementation binds worktree, Git directory, common directory, and object
store descriptors through `/proc/self/fd`, rejects config includes and
extensions, overrides hooks, credentials and commit-graph use, and applies a
Landlock read-only filesystem boundary before Git starts. Repository config is
an inspected local input; it is not claimed to be globally ignored. Hosts
without the descriptor namespace and Landlock ABI 3 support fail closed for
authoritative Git-dependent plans. S3 accepts only SHA-1 object-format
repositories; SHA-256, reftable, and other repository extensions remain
unavailable rather than being interpreted with incomplete config.

Declared status is not implementation approval. Each row must acquire a source
call graph and pass the acceptance matrix before it becomes public-available.

S5 source catalogs may promote exactly the seven Mandatory Core Surface
operations on Linux. That is a source-tree availability bit, not an installed
`dyro-bridge` process. The other implemented services stay
`implemented_testable`. macOS and Windows keep an empty public surface.
The published `0.7.x` wheel does not ship this catalog.

### Mandatory Core Surface

Phase 0 cannot later pass with an empty available catalog. The following
non-empty surface is the Mandatory Core set. In `0.7.x` it is a source
catalog on Linux only; wheel and sdist must not expose it:

- `bridge.hello`;
- `bridge.capabilities.compact`;
- `bridge.operation.schema`;
- `workspace.resolve`;
- `workspace.list`;
- `workspace.observe` as the minimum observation;
- `objective.plan` as the minimum PLAN operation.

Each catalog record carries `must_be_available: true|false` and an availability
state. Unit/negative testing first moves a mandatory operation from `declared`
to `implemented_testable`; source-tree zero-effect gates then permit
`public_available`; wheel and sdist tests must call the public operation before
Phase 0 Go. A zero-operation or discovery-only artifact fails A01.

`task.explain` remains a required product journey but is not part of the minimum
surface until the reviewed Git-read adapter passes B05. Before that it returns
`OPERATION_UNAVAILABLE`, not a possibly wrong explanation.

## 3. Deferred read candidates

| Operation | Tentative class | Why deferred |
| --- | --- | --- |
| `changeset.list` | R0 | Useful but not required for first ten journeys |
| `changeset.verify` | R0 | Invokes Git observation; needs explicit optional-lock and subprocess allowlist proof |
| `task.attempts` | R0 | Requires output privacy and size policy before exposing provenance summaries |
| `task.binding` | R0 | Full review/evidence hashes need a dedicated redacted DTO |
| `task.gates.last_result` | R0 | Gate logs and process output may contain credentials or excessive content |
| `repo.list` | R0 | Remote/path fields need explicit redaction; lower first-slice value |
| `agent.list` | R0 | Current CLI reveals launch argv; a safe availability-only DTO is not yet defined |
| `tool.list` | R0 | Host probing/cache/update behavior must be separated before inclusion |
| `doctor.inspect` | R0 or PLAN | Existing doctor checks may probe tools, Git, or filesystem capabilities; call graph not yet proven pure |

## 4. Explicitly excluded from Phase 0

| Current or proposed operation | Maximum class | Current behavior/risk | Decision |
| --- | --- | --- | --- |
| `task.gates` / `task.gates.run` | R2 | Starts Profile argv, writes `gate-*.log`, appends ledger | Excluded; never alias as R0 |
| `task.answer` | R2 | May reserve, create attempt/worktree, launch Agent, run gates, mutate quality state | Excluded; future split still needs review |
| `task.run`, `task.next`, `task.loop`, `task.daemon` | R2 | Starts execution and mutates task/provenance state | Excluded |
| `task.review` | R2 | Starts reviewer and writes review state | Excluded |
| `task.claim*`, `task.evidence*` | R1/R2 | Mutates claims, evidence generations, pointers, ledger | Excluded |
| `objective.apply`, `continue` | R2/R3 contextual | Acquires authority, creates intents/starts and invokes Task mutations | Excluded |
| `trigger.probe` | R0/R2 contextual | Provider may perform external observation; network/process policy not uniform | Excluded until typed provider review |
| `workspace.default/remove` | R1 | Mutates global registry | Excluded |
| `line.create`, `hotfix.create` | R2 | Creates branches/worktrees and line manifests | Excluded |
| `task.create`, `task.status`, Objective lifecycle/scope changes | R1 | Mutates control-plane state | Excluded |
| `task.signoff` | R3 | Grants delivery approval | Excluded |
| `task.merge --push` | R3 | Mutates integration branches and optionally remotes | Excluded |
| `key.*`, witness mutation | R3 | Trust, signing, revocation, audit authority | Excluded |
| `update now`, tool/Plugin install | R2/R3 | Installs executable code or changes host integration | Excluded |
| release, publish, cleanup | R3 | External publication or destructive/recovery-sensitive action | Excluded |
| `dispatch` | separate boundary | Outbound advisory workflow, not an inbound control operation | Not part of Bridge |

## 5. Future mutation candidate

`workspace.add` is the only named first candidate for a later, separate R1
experiment because the registry already uses a process lock and atomic replace,
and an identical record can converge on authoritative state. It remains
`future-review`, not Phase 0.

Before it can be exposed, a new ADR must specify:

- a model-invisible host approval capability;
- stable workspace and registry identity;
- canonical input and operation-specific plan read set;
- lock order, linearization point, durable intent/receipt, and uncertain state;
- replay semantics for identical and conflicting request IDs;
- symlink/reparse-point and directory-replacement defenses on every platform;
- authenticated versus claimed actor fields;
- crash, concurrency, path, secret-redaction, and real-host tests.

`line.create`, `task.create`, `task.answer`, Objective apply, sign-off, merge,
push, release, publish, and cleanup are not acceptable first mutation pilots.

## 6. Per-operation evidence template

An operation cannot change from `declared` through `implemented_testable` to
`public_available` until this record is complete:

```yaml
operation_id: workspace.resolve
schema_version: 1
planner_revision: null
maximum_risk: R0
core_service: dyro.bridge.observations.resolve_workspace_observation
source_call_graph:
  - dyro.continuation.resolution.resolve_workspace_readonly
  - dyro.config.load_profile_exact
  - dyro.hub.load_registry_bounded
reads:
  - dyro.toml
  - DYRO_HOME/workspaces.json
writes: []
subprocesses: []
network: []
locks: []
recovery: none
sensitive_fields: []
negative_tests:
  writes: pass-source-unit
  network: pass-source-unit
  subprocess: pass-source-unit
  traceback_ansi_secrets: pass-source-unit
installed_artifact_test: source-wheel-sdist-public-corpus-required
real_codex_test: pending
must_be_available: true
availability_state: public_available
platform_availability:
  linux-ubuntu-24.04: available
  macos-15: declared
  windows: unavailable
```

Any discovered write, recovery, unknown subprocess, network call, or raw secret
path resets availability to `unavailable` until the design and risk class are
reviewed again.
