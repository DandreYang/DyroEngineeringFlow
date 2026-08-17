# Dyro Agent Bridge Phase 0 Protocol

Status: Proposed

Authority: [ADR 0007](../adr/0007-agent-bridge-phase-0.md)

Operation allowlist:
[Agent Bridge Operation Inventory](agent-bridge-operation-inventory.md)

## 1. Scope and normative language

This protocol defines a one-request, one-response JSON boundary for pure Dyro
observations and deterministic non-executable plans. `MUST`, `MUST NOT`,
`SHOULD`, and `MAY` are normative.

Phase 0 has no apply method. A client cannot turn a plan into an action by
changing a field, copying a digest, adding `--yes`, or claiming an actor.

## 2. Process contract

The long-term packaged console entry point is `dyro-bridge`. In this `0.7.x`
train that script is **not shipped**. Source-tree callers may use
`python -m dyro.bridge`; an installed `dyro` wheel must not provide
`dyro-bridge` or `dyro-mcp`.

The packaged name, when a later extra exists, remains:

```text
dyro-bridge
```

The process contract is:

1. read exactly one UTF-8 JSON object from stdin, up to 256 KiB; EOF is the
   one-shot frame delimiter, so a client MUST close its stdin write end after
   the request bytes and MUST NOT wait for a response before doing so;
2. reject trailing non-whitespace bytes and duplicate JSON keys;
3. while stdout is writable, emit exactly one compact UTF-8 JSON object followed
   by one newline on stdout;
4. emit no routine logs, progress, ANSI, traceback, warning, or human help on
   stdout or stderr;
5. close inherited stdin after the request and terminate after the response;
6. never invoke the human CLI parser or a `cmd_*` handler;
7. use exit code `0` for an `ok=true` response, `2` for request/protocol errors,
   `3` for bounded Core observation errors, `4` for unavailable operation or
   dependency, and `5` when stdout closes before a complete response. Exit 5
   performs no retry and emits no traceback; it cannot promise a JSON response
   because the output channel is unavailable.

Each invocation is a dedicated single-request, single-thread process. The
descriptor-level stdout/stderr isolation around Core materialization is
process-global and is not a supported in-process concurrency primitive.

Before JSON decoding, the implementation also rejects nesting deeper than 64,
more than 10,000 decoded value nodes, and numeric tokens longer than 128 bytes.
Escaped surrogate code points are rejected rather than transported across
different Unicode implementations. These limits are protocol-major-1
invariants, not caller-tunable settings.

The transport MUST impose response limits. Phase 0 defaults are 1 MiB for a
response, 100 collection items unless the operation defines a smaller maximum,
64 warnings, and 4 KiB for any user-facing message. Truncation is explicit in
metadata and never cuts a JSON token.

Workspace input is bounded separately from transport output. Before reading,
Phase 0 verifies a safe regular file and enforces these initial ceilings:

| Resource | Maximum |
| --- | ---: |
| `dyro.toml` | 1 MiB |
| global workspace registry | 1 MiB and 500 records |
| one `task.toml` or Objective metadata file | 256 KiB |
| one Objective event journal | 8 MiB and 10,000 events |
| task records considered | 2,000 |
| Objective records considered | 500 |
| aggregate workspace bytes read per request | 64 MiB |
| Core observation deadline | 5 seconds |

Operations may choose smaller limits. A single malformed or oversized record is
component-scoped and must not erase valid siblings. Exhausting a count, byte, or
deadline budget sets `partial=true`, `truncated=true` where applicable, and a
stable failure code; response truncation alone is not a computation budget.

## 3. Request envelope

```json
{
  "protocol": {"major": 1, "minor": 0},
  "request_id": "client-correlation-id",
  "client": {"name": "codex-integration", "version": "0.1.0"},
  "operation": "workspace.resolve",
  "input": {
    "workspace": null,
    "start": "."
  }
}
```

Rules:

- `protocol.major`, `client.name`, `client.version`, `operation`, and `input`
  are required.
- `request_id` is optional, bounded correlation text. It is echoed only when it
  matches `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` and does not match a secret, URL
  credential, or absolute-path pattern; otherwise it becomes `null` with a
  fixed `REQUEST_ID_REDACTED` warning on a successful response. Error responses
  omit warnings and retain `request_id=null`. It is not an idempotency key,
  identity, or authorization credential.
- `start` never performs shell or home-directory expansion. Values beginning
  with `~` are invalid; relative values are resolved only against the server's
  explicit working-directory context.
- Unknown top-level fields are rejected in protocol major 1.
- `operation` must exist and be available in the server-side Exposure Catalog.
- `input` is validated against that operation's exact schema before any
  workspace or registry access.
- Paths are not expanded from environment variables. `start` may be relative to
  the process working directory; an explicit workspace alias takes precedence.
- Phase 0 has no `actor`, `approval`, `confirmation`, `command`, `argv`,
  `shell`, `apply`, or `dry_run` request field.

## 4. Success response

```json
{
  "ok": true,
  "meta": {
    "server_protocol": {"major": 1, "minor": 0},
    "requested_protocol": {"major": 1, "minor": 0},
    "dyro_version": "0.6.x",
    "bridge_version": "1.0",
    "operation": "workspace.resolve",
    "operation_schema_version": 1,
    "planner_revision": null,
    "request_id": "client-correlation-id",
    "event_id": "evt_<server-generated>",
    "capabilities_digest": "sha256:<hex>",
    "partial": false,
    "truncated": false
  },
  "data": {
    "workspace": {"name": "sample"},
    "resolution_source": "default"
  },
  "warnings": []
}
```

`event_id` is generated by the Bridge and is suitable only for correlating
local diagnostics. It does not authenticate a person or host. Absolute paths
are omitted by default; an operation may expose a stable opaque resource ID.

Partial success uses `ok=true`, `meta.partial=true`, component-scoped failures,
and healthy data. It MUST NOT silently substitute another workspace or turn an
invalid local Profile into a default registry result.

## 5. Error response

```json
{
  "ok": false,
  "meta": {
    "server_protocol": {"major": 1, "minor": 0},
    "requested_protocol": {"major": 1, "minor": 0},
    "dyro_version": "0.6.x",
    "bridge_version": "1.0",
    "operation": "workspace.resolve",
    "operation_schema_version": 1,
    "planner_revision": null,
    "request_id": "client-correlation-id",
    "event_id": "evt_<server-generated>",
    "capabilities_digest": "sha256:<hex>",
    "partial": false,
    "truncated": false
  },
  "error": {
    "code": "LOCAL_PROFILE_INVALID",
    "message": "The local Dyro Profile is invalid.",
    "retryable": false,
    "details": {},
    "next_actions": [
      {"kind": "inspect_profile", "label": "Inspect the local Profile"}
    ]
  }
}
```

Error `message` is a bounded presentation string, not a raw exception. Details
use operation-specific allowlisted fields. `next_actions` are semantic actions,
not shell commands.

Typed operation schemas and DTOs are the primary output allowlist. Boundary
pattern detection is defense in depth for known credential, URI, argv and path
forms; an unexpected value that would require changing a PLAN fails closed
rather than changing or re-hashing that plan.

### Transport-error metadata

Failures before a valid envelope exists use the same top-level `ok/meta/error`
shape but allow unknown request-derived metadata to be `null`:

```json
{
  "ok": false,
  "meta": {
    "server_protocol": {"major": 1, "minor": 0},
    "requested_protocol": null,
    "dyro_version": "0.6.x",
    "bridge_version": "1.0",
    "operation": null,
    "operation_schema_version": null,
    "planner_revision": null,
    "request_id": null,
    "event_id": "evt_<server-generated>",
    "capabilities_digest": "sha256:<hex>",
    "partial": false,
    "truncated": false
  },
  "error": {
    "code": "INVALID_JSON",
    "message": "The request is not one valid JSON object.",
    "retryable": false,
    "details": {},
    "next_actions": []
  }
}
```

The parser fills `requested_protocol`, `operation`, schema/planner versions and
`request_id` only after each field is safely parsed and validated. It never
invents an empty operation or treats the server protocol as the requested one.

Required common codes include:

| Code | Meaning |
| --- | --- |
| `INVALID_JSON` | Input is not one valid JSON object |
| `REQUEST_TOO_LARGE` | Input exceeded the transport limit |
| `PROTOCOL_MAJOR_UNSUPPORTED` | Client major is incompatible |
| `PROTOCOL_MINOR_UNSUPPORTED` | Client minor is newer than this server minor |
| `SCHEMA_VALIDATION_FAILED` | Envelope or operation input is invalid |
| `OPERATION_UNKNOWN` | Operation ID is not in this Core catalog |
| `OPERATION_UNAVAILABLE` | Known operation is disabled or dependency is absent |
| `LOCAL_PROFILE_INVALID` | A discovered local Profile exists but is invalid |
| `REGISTRY_INVALID` | Global registry cannot be trusted |
| `WORKSPACE_NOT_REGISTERED` | Explicit alias is unknown |
| `REGISTERED_ROOT_STALE` | Registry entry no longer resolves to a valid Profile |
| `HOST_READ_PERMISSION_REQUIRED` | Host sandbox cannot read the selected resource |
| `AMBIGUOUS_WORKSPACE` | Multiple candidates require explicit selection |
| `WORKSPACE_NOT_FOUND` | No local or usable registered workspace exists |
| `OBSERVATION_PARTIAL` | A requested atomic projection cannot tolerate a partial component |
| `RESOURCE_LIMIT_EXCEEDED` | A file, record-count, or aggregate-byte budget was exhausted |
| `OBSERVATION_DEADLINE_EXCEEDED` | The bounded Core observation deadline elapsed |
| `RECORD_INVALID` | One bounded workspace record is invalid; healthy siblings may remain |
| `INTERNAL_ERROR` | Redacted unexpected failure; never includes raw exception text |

For one protocol major, a client minor less than or equal to the server minor is
accepted. A future client minor fails explicitly; the Bridge never silently
downgrades it. Each version component is a non-negative RFC 8785 safe integer
(`0..9007199254740991`); values outside that domain fail envelope validation and
are never reflected into response metadata.

## 6. Compact capability discovery

`bridge.hello` returns only version and protocol compatibility.

`bridge.capabilities.compact` returns entries shaped as:

```json
{
  "operation": "workspace.resolve",
  "kind": "inspect",
  "maximum_risk": "R0",
  "available": true,
  "operation_schema_version": 1,
  "planner_revision": null
}
```

The compact response MUST NOT embed full JSON schemas. The canonical compact
list is hashed into `capabilities_digest`. A client may cache a schema only by
`protocol major + operation ID + operation schema version + capabilities
digest`.

At the S5 boundary, Linux Ubuntu 24.04 reports exactly the seven Mandatory Core
Surface operations as available. The Linux compact digest is
`sha256:426aaee45de4da518fcad5c89ab85ce129662e6af2faff37c705b717a4311e8a`.
macOS 15 and Windows report no available Phase 0 operations and retain the
fail-closed digest
`sha256:3a008d6baa65db697eb44a9a910c4791eb0a96f58fcd361784341c4140ab2bd7`.
These values are protocol fixtures, not a host-side override.

`bridge.operation.schema` accepts one operation ID and returns its request and
response schemas. It rejects unavailable, excluded, and mutation operations.

## 7. Workspace resolution response

`workspace.resolve` returns:

```json
{
  "workspace": {
    "id": "workspace:<stable-opaque-id>",
    "name": "sample",
    "profile_schema_version": 1
  },
  "resolution_source": "explicit",
  "health": "available"
}
```

`resolution_source` is exactly one of `explicit`, `local`, `default`, or
`unique`. Phase 0 does not return registry file paths, Profile absolute roots,
remote URLs, adapter argv, or environment values.

The opaque ID implements ADR 0007 `WorkspaceIdentityV1`: it is stable only while
the canonical Profile root and validated Profile name are unchanged. Moving or
renaming a workspace intentionally changes it. `ConfigRevisionV1` hashes the
bounded exact `dyro.toml` bytes with its domain separator. Neither identifier is
an authentication credential, and neither source payload is returned.

## 8. Observation response

Observation DTOs include:

- `observed_at` in UTC;
- an opaque capture ID and semantic revision digest;
- `complete | partial` completeness;
- bounded typed projections;
- component failures using stable codes.

Every Task/Objective projection includes `integration_inspection` with one of
`complete`, `not_inspected`, or `partial`. A summary with `not_inspected` may
report stored status and dependency facts but MUST omit final
`dispatchable=true|false`, `ready=true|false`, and integration-blocked claims.
Authoritative `task.explain`, `objective.status`, and plans require the reviewed
optional-lock-disabled Git adapter; they remain unavailable before B05 passes.
The Phase 0 Core service is reached through the single-request transport. Its
descriptor-bound Git adapter starts one exact isolated Python binder process,
retains only the reviewed worktree, Git directory, common directory, and object
store descriptors plus a close-on-exec error channel, applies a Landlock
read-only filesystem ruleset, rejects config includes and extensions, overrides
hooks, credentials and commit-graph use, and then executes an allowlisted system
Git read. Repository config remains a validated local input; the protocol does
not claim that `rev-parse` or `merge-base` ignores it. Host integrations must
spawn the one-shot transport rather than import and invoke planning services in
process. On Linux, repository discovery is pinned to those descriptors through
`/proc/self/fd`. A host without both the verified
descriptor namespace and Landlock ABI 3 support returns
`OPERATION_UNAVAILABLE` for authoritative Git-dependent plans. Phase 0 accepts
only SHA-1 object-format repositories; extended formats return a stable
fail-closed error before Git starts. Object alternates outside the approved
directory objects, lazy fetch, replace objects, and more than 100 Git process
starts per request fail closed.

This is a bounded cooperative-state observation, not a filesystem attestation.
As defined by ADR 0007, an actively malicious process with the same operating-
system identity can still replace a ref or object during the read and restore
it afterward; defending that case requires an immutable filesystem snapshot or
external broker and is outside Phase 0. Plan digests do not upgrade this trust
model.

The semantic revision excludes the observation clock so identical facts can
share a digest. A DTO never serializes an internal dataclass recursively; every
field is explicitly copied through the operation schema.

## 9. Plan response

```json
{
  "executable": false,
  "authorization": "none",
  "protocol_major": 1,
  "operation": "objective.tick",
  "operation_schema_version": 1,
  "planner_revision": "objective-tick/1",
  "workspace": {
    "id": "workspace:<stable-opaque-id>",
    "config_sha256": "sha256:<hex>"
  },
  "normalized_input": {"objective_id": "release-readiness"},
  "read_set": {
    "observed_at": "2026-08-06T12:00:00Z",
    "integration_inspection": "complete",
    "execution_mode": "local",
    "objective": {
      "id": "release-readiness",
      "revision": 4,
      "event_sequence": 4,
      "contract_sha256": "sha256:<hex>",
      "scope_sha256": "sha256:<hex>",
      "event_sha256": "sha256:<hex>",
      "operator_state": "active",
      "completion_rule": "all_targets_integrated",
      "requested_mode": "supervised",
      "operations": ["execute", "review"],
      "scope": ["TASK-42"],
      "targets": ["TASK-42"],
      "budget": {"max_actions": 10, "max_attempts_per_task": 2, "max_failures": 2, "max_no_progress_cycles": 2, "max_parallel": 1, "deadline": null}
    },
    "tasks": [
      {
        "id": "TASK-41",
        "line_id": "release",
        "contract_sha256": "sha256:<hex>",
        "status": "pending",
        "depends_on": [],
        "blocked_on": [],
        "external_claim_active": false,
        "integration_state": "not_required",
        "integration_checks": [],
        "active_conflict_task_ids": [],
        "conflict_slot": null,
        "execution_slot": "agent-slot:1",
        "review_slot": "agent-slot:2",
        "merge_slot": "line-slot:1"
      },
      {
        "id": "TASK-42",
        "line_id": "release",
        "contract_sha256": "sha256:<hex>",
        "status": "pending",
        "depends_on": ["TASK-41"],
        "blocked_on": [],
        "external_claim_active": false,
        "integration_state": "not_required",
        "integration_checks": [],
        "active_conflict_task_ids": [],
        "conflict_slot": null,
        "execution_slot": "agent-slot:1",
        "review_slot": "agent-slot:2",
        "merge_slot": "line-slot:1"
      }
    ],
    "decisions": [],
    "capacity": {"max_parallel": 1, "active_parallel": 0, "available_parallel": 1}
  },
  "projection": {
    "selected_actions": [],
    "blocked": [
      {"kind": "execute_task", "subject_id": "TASK-42", "reason": "DEPENDENCY_PENDING", "predicates": {"has_pending_dependency": true, "related_subject_ids": ["TASK-41"]}}
    ],
    "attention": [],
    "tick_wave": [],
    "deferred": [],
    "non_mutating_actions": []
  },
  "effects": [],
  "warnings": [],
  "maximum_risk": "PLAN",
  "effective_risk": "PLAN",
  "expires_at": "2026-08-06T12:05:00Z",
  "plan_sha256": "sha256:<hex>"
}
```

The shown `read_set` and `projection` are illustrative; each plan operation owns
its exact typed schemas. `projection` carries the operation's selected,
blocked, graph, attention, or wave result rather than forcing those facts into a
generic effect list.

Sensitive executor, reviewer and conflict-group values never cross the
boundary. When their equality affects planning, the read set uses deterministic
snapshot-local equivalence tokens such as `agent-slot:1` or `conflict-slot:2`;
renaming a hidden raw value without changing the relation therefore does not
leak or perturb the visible plan.

All request-derived and Core-derived fields first pass the operation allowlist,
size limits, and deterministic redaction. `plan_sha256` is then computed from
RFC 8785 canonical bytes of the final transport-safe plan payload, excluding
only `plan_sha256`. No pre-redaction or hidden field contributes to the digest.
It detects drift and cache corruption only. The Bridge offers no endpoint that
consumes it.

A subject in `blocked` cannot also appear in `selected_actions`, `tick_wave`, or
a `would_*` effect in the same plan. Contract tests reject contradictory
projections rather than asking clients to infer precedence.

## 10. Redaction and audit

Before serialization, request-derived strings, Core exceptions, warnings, and
diagnostics pass a common size and secret guard. Default responses exclude:

- absolute filesystem paths;
- environment variables and their values;
- adapter or gate argv;
- raw prompt, answer, handoff, receipt, review, and log text;
- remote URL userinfo/query and embedded credentials;
- stdout/stderr and Python exception messages.

If diagnostics are persisted in a future stage, the record distinguishes
`claimed_client` from an authenticated principal. Phase 0 has no authenticated
human principal and MUST record `authorization=none`, never “user confirmed.”

## 11. Compatibility

- Protocol major changes are incompatible and fail closed.
- Minor changes may add optional response fields but cannot add an operation to
  a client's granted tool list.
- Operation schema changes that alter validation or meaning increment that
  operation's schema version.
- Planner behavior changes increment `planner_revision` and produce a new plan
  digest.
- A host integration advertises an explicit supported protocol and schema
  range. Core and integration version skew is tested in both directions.

## 12. MCP mapping after Phase 0 Core approval

MCP is an adapter over the same Core services, not a separate operation engine.
The initial mapping is small and typed, for example:

- `dyro_hello`
- `dyro_workspace_resolve`
- `dyro_workspace_observe`
- `dyro_task_list`
- `dyro_task_explain`
- `dyro_objective_plan`

No MCP tool name contains `execute`, `apply`, `run`, `answer`, `gate`, `review`,
`signoff`, `merge`, `push`, `release`, `publish`, or `cleanup` in Phase 0.
