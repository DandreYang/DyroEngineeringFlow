# ADR 0003: Zero-friction global home

## Status

Proposed

## Context

Dyro already owns delivery lines, task worktrees, gates, evidence, review, and
merge controls. Its daily entry point still requires the user to start inside a
directory containing `dyro.toml`, or to remember `--root`. That makes the
control plane safer than a project-specific launcher, but slower to enter.

The primary user journey must not require a newcomer to understand Profile,
anchor, adapter, linked worktree, gate, receipt, or sign-off terminology.
Those concepts remain available to operators, but they are not prerequisites
for opening an existing piece of work.

## Decision

Dyro will add a small global home layer above the existing workspace-scoped
Core:

1. A global registry stores workspace aliases and absolute Profile roots. It
   stores paths and recent UI choices only, never credentials or delivery
   authority.
2. Running `dyro` without a subcommand resolves the current Profile first,
   then the registered default/recent workspace, and presents action-oriented
   choices for existing development lines, Hotfixes, and task worktrees.
3. `dyro task open` opens an existing task worktree without running the task or
   changing task state.
4. `dyro status --all` reports every registered workspace without changing it.
5. Agent discovery distinguishes configured/launchable adapters from commands
   that are merely installed. Discovery never grants an unreviewed provider
   execution authority.
6. An explicit `--workspace <alias>` selects a global workspace. Existing
   `--root` behavior remains supported and takes precedence only when chosen
   explicitly; the two selectors are mutually exclusive.
7. Workspace registration is reversible and guarded by atomic writes and a
   process lock. A malformed registry fails closed and is never overwritten.

## User experience contract

### First use

`dyro` explains how to set up the current project or register an existing Dyro
workspace. It does not emit the low-level `dyro.toml not found` error as the
primary message, and cancellation leaves no state behind.

### Daily use

From any directory, `dyro` shows the selected project, defaults to the most
recent valid target, and reuses the most recent available Agent. A configured
single Agent is selected automatically. Before launch it prints the exact
workspace and command.

### Failure recovery

An unavailable workspace, missing task worktree, or missing Agent explains
what happened and gives one concrete recovery command. Existing worktrees and
uncommitted changes are never cleaned, stashed, or rewritten by the home.

## Non-goals

- Replacing or modifying another workspace launcher in this stage.
- Importing worktrees owned by external tools automatically.
- Creating task worktrees from the home without the existing task controls.
- Allowing local Agent discovery to bypass Profile adapter policy.
- Replacing gates, evidence, review, sign-off, merge, or push controls.

## Acceptance criteria

- From an unrelated directory, a registered user can run `dyro`, accept the
  default target, and open it with at most one selection.
- No configuration file editing is required for workspace registration.
- `dyro task open` never changes task status.
- `dyro status --all` remains read-only when some registered paths are stale.
- Dry-run never writes global recent state or launches an Agent.
- Registry corruption, duplicate paths, and unsafe state-file shapes fail
  closed with actionable errors.
