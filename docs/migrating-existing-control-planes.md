# Migrating an Existing Engineering Control Plane

DyroEngineeringFlow is intended to become a complete, reusable engineering control plane. It can replace an existing launcher and dispatcher only after its generic Profile and extensions reproduce the controls that protect that team's delivery process.

## Preserve the source of truth during migration

Do not overwrite an existing control plane or use two orchestrators to mutate the same task worktree. Start with a read-only Profile that mirrors repository anchors, active delivery lines, task metadata, and required checks. Keep the existing launcher and dispatcher as the only write path until parity is demonstrated.

Avoid broad repository discovery in a mature workspace. A workspace often contains repository anchors as well as release, hotfix, and task worktrees; importing all Git directories would register duplicates. Import only the declared repository anchors through a deterministic manifest or an explicit repository list.

## Generic capability mapping

| Existing control-plane concern | Dyro target capability |
| --- | --- |
| Repository registry and workspace layout | Profile repository registry, mounts, storage modes, and doctor extensions |
| Feature lines and production repairs | Per-repository baselines, explicit delivery-line manifests, Hotfix policy, and Change Sets |
| Launcher / IDE routing | Agent adapters with declared capabilities, launch context, and workspace access rules |
| Task dispatcher | Dependency-aware task state machine, conflict groups, gates, receipts, reviews, and ledger |
| Trusted execution | Isolated runner policy, one-time task claims, evidence import, timeout watchdog, and captured evidence |
| Review and sign-off | Receipt- and repository-HEAD-bound review envelopes plus an explicit external-sign-off state |
| Release and backport governance | Promotion rules, approval records, per-repository delivery evidence, and forward-port tracking |

## Safe migration sequence

1. Create an import manifest from the existing registry; pin every repository's ref and storage mode.
2. Run doctor parity checks without creating worktrees or launching agents.
3. Use a new, low-risk task to compare dry-run output: worktrees, branches, gates, permissions, and evidence locations must match the existing system.
4. Execute one pilot task only in an approved isolated runner. Keep the legacy dispatcher as the fallback and do not run both against the task.
5. Compare task receipt, review evidence, merge result, promotion record, and rollback path.
6. Make the legacy launcher a thin compatibility wrapper only after all acceptance criteria pass. Retire the legacy dispatcher last.

## Replacement acceptance criteria

Dyro must not be called a replacement until it can prove all of the following for the importing team:

- each repository uses its own verified base ref or immutable commit when required;
- every delivery line declares its storage mode and the doctor rejects an unexpected Git topology;
- automated execution cannot bypass the team's isolation and approval policy;
- review cannot transition a task to done without evidence bound to the executed receipt and exact per-repository task HEADs;
- release, rollback, and forward-port obligations are recorded per repository;
- all custom policy stays in a private Profile or extension, never in the Dyro core.

## Core and Profile boundary

The core owns mechanisms: schemas, state transitions, Git safety checks, process isolation contracts, adapters, and evidence formats. A Profile or extension owns organization-specific repository names, infrastructure, approval rules, delivery platforms, compliance requirements, and business checklists.
