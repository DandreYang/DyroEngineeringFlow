# ADR 0004: Native continuation engine

## Status

Proposed

## Context

Dyro already controls the delivery topology of a multi-repository workspace:
TaskGraph dependencies, conflict groups, task state, bounded execution attempts,
gates, review, sign-off, merge, and evidence. Its existing `task next`, `task
loop`, and `task daemon` commands can schedule work that is ready now.

The remaining gap is temporal continuity. A delivery objective may need to wait
for an answer, a decision, a time boundary, an external receipt, or a remote
state change; resume after a process or machine restart; stop when a budget is
exhausted; and explain why it is waiting. Repeatedly launching an unbounded
agent loop would weaken Dyro's evidence and authority boundaries.

## Decision

Dyro will add a native continuation engine above the existing TaskGraph and
task mutation APIs.

1. An `Objective` is a durable intent envelope that references a pinned set of
   Task IDs in one delivery line. It does not redefine tasks, dependencies,
   gates, or completion evidence.
2. Every scheduling cycle builds one immutable `ContinuationSnapshot` from the
   Objective contract, the compiled TaskGraph, task status and provenance,
   decisions, claims, trigger observations, budgets, policy, and the clock.
3. A pure planner converts the snapshot into a `ContinuationPlan`. Planning and
   explanation never write state, start an Agent, consume a budget, or probe a
   network.
4. Mutating plans are applied as bounded, journaled `ActionIntent` records.
   Existing `run_task`, `review_task`, and `merge_task` remain the only delivery
   mutation paths after review acceptance and merge are separated into distinct
   operations. Review can never implicitly merge or push. An uncertain action
   is never silently retried.
5. `Trigger` providers may observe when work should be reconsidered. They can
   wake the planner but cannot mark a task complete, accept evidence, sign off,
   merge, or push.
6. Hierarchical `Budget` limits bound actions, attempts, failures, elapsed time,
   concurrency, no-progress cycles, and optional provider-reported usage.
7. `AttentionItem` is a read-only projection of authoritative state. It gives a
   newcomer one recommended next action without becoming another task store.
8. Automatic operation requires a time-bounded local `ActivationLease`, an
   allowing workspace policy, an allowing Objective contract, and the existing
   task-level permissions. Push remains explicit in the first release.
9. Existing scheduling commands and Objective commands share one scheduler
   snapshot and planner. Dyro will not maintain two readiness engines.
10. Observe-only Objectives may overlap, but only one active Objective may own
    mutation authority for a Task and its dependency closure. Legacy unattended
    batch commands cannot bypass that ownership, the action journal, or budgets.
11. External execution and review require purpose-separated signatures whose
    trusted keys bind immutable principals. An execution principal cannot
    review its own result; when sign-off is required, it is signed and its
    principal is also independent under workspace policy.
12. Every action publishes a durable start record before its first side effect.
    Process-start records use a launch barrier so the target command cannot run
    before its process identity reaches durable storage.
13. Scheduler ownership uses a monotonically increasing fencing generation.
    Intent, start, receipt, budget, policy, accepted scope, and activation all
    bind to that generation or to a previously fenced action that already
    crossed the durable start point.

## Authority model

The effective authority for an action is the intersection of:

- workspace policy;
- Objective contract and accepted revision;
- current local activation mode and lease;
- task contract permissions;
- valid task state, evidence, and graph constraints.

No adapter, trigger provider, host scheduler, or external executor can widen
that intersection. External execution mode remains planning-and-observation
only on the control machine; imported evidence must pass the existing claim,
signature, provenance, gate, and review checks. Evidence import remains an
explicit control-plane operation and is not exposed to the continuation engine.

## Safety invariants

1. `task.toml` and the compiled TaskGraph remain the only delivery graph truth.
2. Objective targets are references, not copied Task definitions.
3. Objective completion is derived from valid evidence and integration of all
   targets, not from an agent message or a mutable status string.
4. Dry-run and plan commands are observably read-only, including global recent
   state and budget counters.
5. One objective has at most one active scheduler owner; existing task and
   merge locks remain authoritative below that owner.
6. Every started mutation has a durable intent and idempotency key before the
   operation begins.
7. A durable start record is the side-effect linearization point. Revocation
   cancels an intent that has not crossed it; an action that has crossed it may
   only finish with a verified receipt or become `uncertain`.
8. A crash between durable start and receipt produces `uncertain`, not an
   automatic duplicate execution.
9. Contract drift, invalid graphs, corrupted journals, dirty worktrees,
   unknown hard-cost usage, expired authority, and clock anomalies fail closed.
10. Automatic local merge requires workspace, Objective, and task permission;
   automatic push is not supported in the first release.
11. Presentation state cannot release a dependency, satisfy a gate, or change
    delivery status.
12. Trigger satisfaction wakes planning or creates attention; it never releases
    a TaskGraph dependency, decision, gate, review, or evidence requirement.
13. Objective operator state is only `active`, `paused`, or terminal `stopped`.
    Completion and repair are revalidated derived results, never directly
    writable lifecycle values.

## User experience

The default path requires no configuration-file editing:

```bash
dyro objective start
dyro continue
```

The start wizard selects the current line, previews the currently pinned
targets, applies supervised safe defaults, and explains what will never happen
automatically. Running bare `dyro` from any directory shows the active
Objective first, grouped as `ready`, `waiting`, `needs you`, `paused`, or
`repair required`, with one recommended action. Opening a workspace continues
to use the existing coding-tool picker; executing a Task uses the adapter fixed
by that Task contract.

Advanced operators can use `objective plan`, `objective explain`, structured
JSON output, explicit budgets, trigger providers, and a foreground daemon.

## Compatibility and rollout

- Workspaces without an Objective behave exactly as before.
- Objective support starts in observe-only mode, then supervised mode, and only
  later enables explicitly leased automatic mode.
- `v0.5.7` remains a maintenance-only line; it does not carry either the
  continuation engine or the Console.
- `v0.6.0` is the first unified feature release: it contains the continuation
  engine and the local read-only Console. Automatic operation still remains
  disabled by default and requires its own explicit ActivationLease; the
  Console never gains browser mutation authority in that release.
- Existing Task manifests and evidence formats remain valid.
- Existing `task next`, `task loop`, and `task daemon` become compatibility
  surfaces over the shared scheduler primitives rather than a separate engine.
- Host integration consumes a host-neutral `next_wake_at` contract; no platform
  service manager becomes a Core dependency.

## Consequences

Dyro gains recoverable, budgeted, explainable continuation without becoming a
generic agent loop or delegating control-plane authority. The cost is a new
durable Objective journal and a stricter recovery model. The implementation
therefore remains staged even though it is released together: plan-only
behavior must be proven before supervised mutation, and supervised mutation
must be proven before unattended operation. The unified `v0.6.0` release gate
must also prove that the Console is still read-only and consumes only
Core-owned projections.

## Non-goals

- A second task list, dependency graph, or completion state machine.
- Autonomous product planning or generation of business acceptance criteria.
- A general-purpose cron service or unrestricted command-probe framework.
- Automatic credential provisioning, remote-repository creation, release, or
  push.
- Treating model consensus, trigger output, or external status as delivery
  evidence.

## Acceptance criteria

- One Objective can be created and resumed from any directory without editing
  TOML manually.
- A plan is deterministic for the same canonical snapshot and explains every
  selected or blocked action with a stable reason code.
- Restarting after a completed action resumes from its receipt; restarting
  after an uncertain action stops for repair and does not duplicate it.
- Review acceptance never invokes merge or push; merge is a separately planned
  and separately authorized action.
- External completion rejects unsigned, self-reviewed, or policy-conflicting
  reviewer and sign-off identities.
- Owner takeover, pause, stop, reconcile, policy changes, and activation expiry
  are fenced between intent and durable start; an old owner cannot start work.
- Time, decision, task-state, manual-signal, and extension-provided triggers
  obey bounded backoff and cannot mutate delivery state.
- Budget exhaustion, no progress, contract drift, invalid graphs, and expired
  activation all stop automatic mutation and create actionable attention.
- Automatic actions cannot bypass task locks, conflict groups, evidence,
  review, sign-off, merge policy, or external-execution restrictions.
- Bare `dyro` presents the current Objective and one safe next action before
  exposing advanced controls.
- New Objective commands resolve a workspace from explicit selection, the
  current Profile, or the registered Home; ambiguous non-interactive use fails
  with an explicit selector requirement.
- Source, tests, examples, changelog, generated help, branch metadata, and
  release artifacts pass the repository terminology policy scan.
