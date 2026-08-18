---
name: dyro-task-merge
description: >
  Preflight whether a Dyro task can merge into its owning development line.
  Use when the user runs /dyro-task-merge. Never execute the merge.
disable-model-invocation: true
user-invocable: true
argument-hint: "[workspace-alias] [task-id]"
metadata:
  short-description: "Preflight task → line merge; do not merge"
---

# Dyro Task Merge

Preflight only. This is not a first-party Dyro integration and not `task review` PASS.

`task merge` means **done task branch → owning development line**.
It does not mean line → `main`, release, tag, or PyPI.

If the user said 合入主线 and means git `main`, say Dyro has no such command here and stop **before** any preflight.

## Hard boundary

Do not run any of:

- `dyro task merge` with `--yes`
- `git merge` / `git switch` / `git checkout`
- `task signoff`, `task gates`, `task review`, `task run`
- `task status <id> <value>` (the write form)
- `objective apply`, `dispatch`, `console`, push, publish

Do not invent `--yes` or `--push`. Do not restore a drifted line branch.
Do not add `--include-paths`.

A 会审 Go, a green test log, or an empty `next.commands` is not permission to merge.

## Resolve workspace

1. If the user named both an alias and a task id, use them as named.
2. If only one token is given: treat it as a workspace alias when `dyro workspace list --format json` contains that name; otherwise treat it as a task id.
3. If no alias, run `dyro workspace list --format json`. If more than one workspace is `available`, ask once; do not guess.
4. Keep `--workspace <alias>` on every later command.

## Allowlisted reads

```bash
dyro --workspace <alias> doctor --format json
dyro --workspace <alias> status --format json
dyro --workspace <alias> next --format json
dyro --workspace <alias> line list --format json
dyro --workspace <alias> task list
dyro --workspace <alias> task status <id>
dyro --workspace <alias> --dry-run task merge <id>
```

`task status` is read-only only when no status value is passed.
Never pass `--yes` to `task merge`. `--dry-run task merge` is the merge-gate preflight (review, receipt, task HEAD, signoff, line branch, dirty line).
Prefer JSON when the command accepts `--format json`. One JSON document only; `kind=error` is blocked evidence.

## Preflight

1. `doctor`. Any `FAIL` → stop.
2. `status`. The task's line row must match the registered branch and `dirty_count` must be 0. Otherwise stop.
3. Choose the task:
   - Use the task id in the slash arguments if present.
   - Else from `task list`, take the unique `done` task on the intended line.
   - If several `done` tasks or none, ask once.
4. `task status <id>` must be `done`. Any other status → stop.
5. `dyro --workspace <alias> --dry-run task merge <id>`. Non-zero exit or `kind=error` → stop and quote the CLI error. Do not proceed.

## Report

Use Observed / Inferred / Unknown / Plan / User action.

If preflight failed, `User action` is only:

```bash
dyro --workspace <alias> doctor
```

or the `--dry-run task merge` that failed. Do not print a live merge command.

If every preflight step passed, say clearly that `next.commands` did not emit this, then show **one** command for the user to run personally:

```bash
dyro --workspace <alias> task merge <id> --yes
```

Do not add `--push`. Do not run that command.
