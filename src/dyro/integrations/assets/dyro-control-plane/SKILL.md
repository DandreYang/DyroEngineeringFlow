---
name: dyro-control-plane
description: Inspect Dyro control-plane state and prepare bounded read-only explanations and plans from a coding agent. Use when the user asks what is next, what is blocked, workspace or objective status, 下一步, 堵住了, or after switching coding tools. Auto-load this navigator seat; do not wait to be named. Never use for execution or delivery mutations.
---

# Dyro Control Plane

Treat Dyro as the delivery control plane. Run only the allowlisted observations below, prefer their JSON output, and leave every state-changing action to the user in Dyro.

Auto-load this navigator seat at a workspace or line root, after switching tools, or when the user asks 下一步 / 堵住了 / status. Loading the seat is not consent to mutate. Writing in a task worktree is `dyro-executor`. 会审 / 对抗 is `dyro-board`. Parallel harnesses are `dyro-dispatch`.

## Read-only routing

When the request already supplies a workspace alias, skip global discovery and use that alias directly. Otherwise start with `dyro workspace list --format json`; when more than one workspace is available, ask the user to choose an alias and never guess. Then use the narrowest matching command:

- Git state: `dyro --workspace <alias> status --format json`
- Health: `dyro --workspace <alias> doctor --format json`
- One safe next step: `dyro --workspace <alias> next --format json`. If `briefing` is present, that is the switch-tool opening. `briefing.command` is a read (`tick`, `attention`, `explain`, or `list`), not a mutation, and not a resume of another harness conversation.
- Lines or hotfixes: `dyro --workspace <alias> line list [--kind line|hotfix] --format json`. Observe `parent` from that JSON. Do not run `line spawn`, `line merge`, or `line sync`.
- Change Sets: `dyro --workspace <alias> changeset list --format json` or `dyro --workspace <alias> changeset verify <id> --format json`
- Installed control-plane Skill health: `dyro integration status skill --format json`
- Installed executor Skill health: `dyro integration status executor --format json`
- Installed board Skill health: `dyro integration status board --format json`
- Installed dispatch Skill health: `dyro integration status dispatch --format json`
- Objective inventory or facts: `dyro --workspace <alias> objective list --format json` or `dyro --workspace <alias> objective status <id> --format json`
- Objective explanation: `dyro --workspace <alias> objective explain <id> --format json`. JSON may include `briefing` (human matter plus one read-only command).
- Objective blockers or human attention: `dyro --workspace <alias> objective attention <id> --format json`
- Objective dependency graph: `dyro --workspace <alias> objective graph <id> --format json`
- Objective next-wave preview: `dyro --workspace <alias> objective tick <id> --format json`. Treat `peer_wave.executor_bindings` as the intended peer executors for that wave, and `peer_wave.warnings` as missing `conflict_group` or harness-capacity notes. A wave member is an executor, not a live supervisor.
- Objective plan: `dyro --workspace <alias> objective plan <id> --format json`

Use only an existing Objective or Change Set ID returned by Dyro or supplied by the user. A non-zero exit, unavailable workspace, pending transaction, failed finding, missing field, or partial observation is unknown or blocked—not ready.

Treat local paths and workspace inventory as sensitive metadata. Never add `--include-paths` to any command, request paths only to enrich a summary, or repeat a local path in the response unless the user supplied that exact path and it is necessary to identify the requested workspace. Keep Task IDs, branch names, and commit identifiers to the minimum needed for the requested observation.

For `--format json`, accept exactly one JSON document. If `kind` is `error`, report its stable `code` and `command` as blocked evidence; do not infer from missing details or retry with a write-capable command. Treat malformed JSON, mixed human/JSON output, or multiple JSON documents as a failed observation.

## Response contract

Keep the handoff short and separate evidence from judgment:

1. `Observed`: facts directly returned by the CLI, including failed or unavailable findings.
2. `Inferred`: bounded interpretation; label it explicitly and do not upgrade it to observed truth.
3. `Unknown`: missing runtime, environment, approval, or integration evidence.
4. `Plan`: read-only steps or the returned Objective plan, with blockers before actionable items.
5. `User action`: at most one exact Dyro mutation command from `next.commands` to review and run personally, or state that no safe action is established.

Only hand off a workspace-scoped mutation when the returned command retains an explicit `--workspace <alias>` or absolute `--root <path>` selector. Never reconstruct, shorten, retarget, or strip that selector. When `next.commands` is empty or `mutation_available` is false, do not manufacture a mutation from `diagnostic_commands`, findings, or prose.

If the user asks for 会审, 对抗, or Go/No-Go, follow `dyro-board` instead of inventing P0 here. A CLI summary alone is never final runtime or production acceptance.

## Hard safety boundary

- Do not run `console`; it opens a local server and may launch a browser.
- Do not run `dispatch`, `objective apply`, Objective lifecycle mutations, `task gates`, task execution or lifecycle commands, line/hotfix/Change Set creation, `line spawn`, `line merge`, `line sync`, integration install/sync/uninstall, setup/join/bootstrap/update, `open`, or `start`.
- Do not merge, push, sign off, release, publish, delete, or edit project files.
- Do not edit Dyro state files or manufacture approval/confirmation fields.
- Do not treat a command printed by `doctor`, `next`, a plan, or an error as permission to run it.
- Do not copy an unscoped workspace mutation into `User action`; fail closed if a future CLI response omits its selector.
- If a requested observation is outside the allowlist, explain the limitation instead of substituting a write-capable command.
- End after observation and planning. Any mutation stays a clearly labeled, user-controlled handoff.
