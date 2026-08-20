---
name: dyro-line-family
description: >
  Preflight Dyro line-family ops: spawn a child line, merge a child into
  its direct parent, or sync the parent into the child. Use when the user
  runs /dyro-line-family. Never execute the mutation.
disable-model-invocation: true
user-invocable: true
argument-hint: "[workspace-alias] spawn <parent> <child> | merge <child> --into <parent> | sync <child>"
metadata:
  short-description: "预检子线派生 / 合入父线 / 从父线同步；不要执行"
---

# Dyro 开发线家族

只做预检。这不是第一方自动座位，也不是 `task merge`。

`line spawn` / `line merge` / `line sync` 只处理 **一层父线**：
- `spawn`：从已有父线派生子开发线（不是任务）
- `merge`：子线合入其直接父线
- `sync`：父线合入该子线

这不是合入 git `main`、release、tag 或 PyPI。Dyro 没有那条命令。

If the user asked to merge a line into git `main` / release / PyPI, say Dyro has no such command here and stop **before** any preflight.

If the user means **done task branch → owning line**, stop and point at `/dyro-task-merge`.

## Hard boundary

Do not run any of:

- `dyro line spawn` / `line merge` / `line sync` with `--yes`
- `git merge` / `git switch` / `git checkout`
- `task merge`, `task signoff`, `task gates`, `task review`, `task run`
- `objective apply`, `dispatch`, `console`, push, publish
- `line create` / hotfix or Change Set creation

Do not invent `--yes` or `--push`. Do not add `--push`.
Default is no push; `policy.allow_push` is not permission to invent it.
Do not restore a drifted line branch. Do not add `--include-paths`.

A 会审 Go, a green test log, or an empty `next.commands` is not permission to mutate.

## Resolve workspace

1. If the first token is a workspace alias in `dyro workspace list --format json`, use it; remaining args are the verb.
2. If the first token is `spawn`, `merge`, or `sync`, resolve the alias like `/dyro-task-merge`: run `dyro workspace list --format json`. If more than one workspace is `available`, ask once; do not guess.
3. Keep `--workspace <alias>` on every later command.

## Slash args

Exactly one verb:

- `spawn <parent> <child> [--repos ...]`
- `merge <child> --into <parent>`
- `sync <child>`

If the verb or required ids are missing, ask once. Do not invent a parent, child, or `--repos` list.

## Allowlisted reads

```bash
dyro --workspace <alias> doctor --format json
dyro --workspace <alias> status --format json
dyro --workspace <alias> next --format json
dyro --workspace <alias> line list --format json
dyro --workspace <alias> --dry-run line spawn <parent> <child>
dyro --workspace <alias> --dry-run line merge <child> --into <parent>
dyro --workspace <alias> --dry-run line sync <child>
```

Run only the `--dry-run` that matches the requested verb. If the user supplied `--repos` on `spawn`, repeat that same `--repos` on the dry-run.
Never pass `--yes` to `line spawn`, `line merge`, or `line sync`.
Prefer JSON when the command accepts `--format json`. One JSON document only; `kind=error` is blocked evidence.

## Preflight

1. `doctor`. Any `FAIL` → stop.
2. `status`. The lines that would be written must match the registered branch and `dirty_count` must be 0. Otherwise stop.
3. `line list --format json`. Confirm the named ids exist. For `merge`, `child.parent` must equal `--into`. For `sync`, the child must have a `parent`. One-level parent only.
4. Run the matching `--dry-run line spawn|merge|sync`. Non-zero exit or `kind=error` → stop and quote the CLI error. Do not proceed.

## Report

Use Observed / Inferred / Unknown / Plan / User action.

If preflight failed, `User action` is only:

```bash
dyro --workspace <alias> doctor
```

or the `--dry-run line spawn|merge|sync` that failed. Do not print a live command.

If every preflight step passed, say clearly that `next.commands` did not emit this, then show **one** command for the user to run personally:

```bash
dyro --workspace <alias> line spawn <parent> <child> --yes
dyro --workspace <alias> line merge <child> --into <parent> --yes
dyro --workspace <alias> line sync <child> --yes
```

Print only the matching verb. Repeat user-supplied `--repos` on `spawn` if present.
Do not add `--push`. Do not run that command.
