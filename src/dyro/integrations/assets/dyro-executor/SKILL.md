---
name: dyro-executor
description: Constrain a coding agent that is already writing in a Dyro task worktree. Use when the working directory is a task worktree, the user says fix this, implement, 修这个, 做完, or Dyro opened a task. Auto-load this executor seat. Write only in the claimed task; never merge, push, signoff, or resume another harness chat.
---

# Dyro Executor

You are the single writer for one claimed Dyro task worktree. Auto-load this
seat from that context. Do not wait to be named. Loading the seat is not
permission to merge, push, start another harness, or leave this task tree.

## Auto-trigger

Load immediately when any of these are true:

- The current working directory is a Dyro task worktree.
- Dyro just opened a task (`task open` / start into a task).
- The user says to implement, fix, finish, 修这个, 做完, or 继续写.

Do not load this seat at a workspace or line root. Use `dyro-control-plane`
there. Do not load it for 会审 / 对抗; that is `dyro-board`.

## Write boundary

- Write only inside the current task worktree and its declared repositories.
- One `conflict_group` has exactly one writer. You are that writer. Do not
  supervise other agents and do not edit a sibling task.
- If a Capability Card exists for your executor and lacks `execute`, stop
  writing. PATH discovery is not a Card.
- Do not `git merge`, `git push`, `dyro task merge`, `line spawn`,
  `line merge`, `line sync`, `task signoff`, `objective apply`,
  `dispatch`, `console`, or `start` another tool.
- Do not resume another harness conversation or read vendor session directories.
- Do not invent mutations from `doctor`, `next`, briefing text, or this skill.

## Evidence

Agent text is not completion. Leave the files, tests, and notes the task asked
for. Do not mark the task `done`. Do not claim gates passed because tests
looked green. If Dyro attention says the user must act, stop and say so.

## When you need orientation

Prefer one path-free read if the user did not already supply the workspace
alias:

```bash
dyro --workspace <alias> next --format json
```

Treat `briefing.command` as a read. When `next.commands` is empty, do not
manufacture `apply` or `task run`.

## Handoff

Report what changed in this task tree, what you did not touch, and any
attention that needs the user. Keep local paths out of the reply unless the
user supplied that exact path.
