# DyroEngineeringFlow

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md)

**DyroEngineeringFlow · `dyro` CLI** is a local-first engineering automation and delivery control platform for multi-repository teams. It brings development lines, Git worktrees, agent launchers, task gates, independent review, and merge audit into versioned workspace configuration.

**Keep engineering moving from task to delivery.**

DyroEngineeringFlow is not coupled to Codex, Claude, or any business domain. Each team supplies a `dyro.toml` Profile for repositories, layouts, agent adapters, and delivery policy; business rules, model cost, and release practices stay in that Profile.

## What it enforces

- A task belongs to exactly one development line—never a mixed feature or hotfix workspace.
- Each task runs in its own `git worktree` on a `task/<id>` branch.
- Gates are executed by the orchestrator; an agent's self-report is not evidence of success.
- Review is bound to the execution receipt and exact per-repository task HEADs; source drift invalidates it.
- A task needs independent review before it becomes `done`; merge and push require explicit confirmation by default.
- Executable configuration is represented as argv arrays. The core never runs TOML-provided shell strings.

## Quick start

For daily CLI use, install `dyro` from PyPI in an isolated `pipx` environment (Python 3.11 or later):

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# Open a new terminal after ensurepath, then:
pipx install dyro
dyro --version
```

To upgrade later, run `pipx upgrade dyro`. If your team manages Python packages through `pip` instead, use:

```bash
python3 -m pip install --user --upgrade dyro
```

Place your repositories in a workspace, then use the newcomer path to discover them, create the safe state directories, and create the first development line in one command:

```bash

mkdir my-workspace && cd my-workspace
# Clone or move your Git repositories under this directory first.
dyro setup . --name my-workspace --line dev --yes
```

`setup` scans local Git repositories, records their workspace-relative paths, derives their development-line mounts, and reads `origin` when available—no TOML editing. `--yes` is required only because the first line creates Git worktrees. Use `--no-line` when you want the Profile first and will create a line later. If the workspace has no repositories yet, use the guided fallback:

```bash
dyro init . --wizard --name my-workspace
```

Add a repository later without opening `dyro.toml`:

```bash
dyro repo add repositories/services/payments
dyro repo list
```

Manage common delivery policy and Agent adapters without opening `dyro.toml`:

```bash
dyro config set policy.execution_mode external
dyro config get policy.execution_mode
dyro agent add ci-runner --preset noop
dyro agent test ci-runner
```

If a Profile contains remotes, missing repository anchors can be created safely:

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

For a new teammate, the normal entry point is one command. It checks the workspace, then selects a development line and local agent:

```bash
dyro start
```

## Delivery workflow

Use explicit commands when scripting or leading a release:

```bash
dyro doctor
dyro line create release-2026-10 --base origin/main --yes
# Override the verified base only for repositories that need one.
dyro line create release-2026-10 --base origin/main --repo-base web=v2026.10.0 --yes
dyro open release-2026-10 --agent codex
dyro task create API-101 --title "Implement API contract" --line release-2026-10 --repository api
dyro task next
dyro task next --run --yes
dyro task review API-101
dyro task merge API-101 --yes
dyro changeset create release-2026-10-ready --line release-2026-10
dyro changeset verify release-2026-10-ready
```

A production hotfix must state its verified production base; it never inherits a default branch implicitly:

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

For a Profile whose execution and approval are run by a separate trusted system, set `policy.execution_mode = "external"` and `policy.require_external_signoff = true`. Local Dyro will then allow only planning; a review bound to the receipt and exact task HEADs must be signed explicitly before a task becomes `done`:

```bash
dyro task claim API-101 --by isolated-runner-1
# In the isolated runner: run declared gates and package receipt, logs, and exact HEADs.
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md --output /runner/out/API-101.zip
# In the control plane: validate and import the one portable package.
dyro task evidence execution API-101 --bundle /runner/out/API-101.zip
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
```

Every write-capable operation has a planning mode:

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## Command map

| Command | Purpose |
| --- | --- |
| `setup` / `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | Onboard a teammate without TOML edits, manage anchors, and choose a line and agent. |
| `doctor` / `status` | Validate and display control-plane state. |
| `line create/list` | Create, register, and inspect feature development lines. |
| `hotfix create` | Create a hotfix line from an explicit production base. |
| `changeset create/list/verify` | Pin and verify the exact clean Git heads that make up a multi-repository delivery. |
| `config get/set` / `agent list/add/test` / `open` | Safely manage common policy and adapters, validate an executable, or open an agent in the correct development line. |
| `task create/list/board/status/next` | Manage task manifests, the state machine, and the next actionable task. |
| `task run/answer/gates/review/signoff` | Run tasks, resolve questions, execute gates, request independent review, and record external sign-off when a Profile requires it. |
| `task claim` / `task evidence build/execution/review` | One-time claim, portable execution-evidence build/import, and receipt-bound review import for an external isolated runner. |
| `task merge` | Merge a reviewed task branch into its owning development line. |
| `task loop/daemon/stats/decisions` | Run controlled batches, scheduling, ledger reporting, and decision gates. |

See the [architecture and Profile contract](docs/architecture.md) and the [existing control-plane migration guide](docs/migrating-existing-control-planes.md) for implementation detail.

## Languages and documentation

This README is maintained in English, Simplified Chinese, Japanese, Korean, and Spanish. Commands, configuration keys, directory names, and safety rules are deliberately identical across translations. The current CLI messages and extended technical guides are primarily Chinese; multilingual README support does not claim that the runtime has language switching yet.

## Current boundaries

DyroEngineeringFlow provides a complete local workflow loop and policy controls for keeping stricter teams in planning-only local mode. It does not create remote repositories, ship SaaS credentials, or provision an external runner; it does provide a portable evidence-package contract for one. Local multi-repository merges are preflighted and recovered as one operation; remote Git servers cannot provide atomic cross-repository push, so partial push failure is recorded for recovery. Automatic merge requires permission in both the task manifest and local policy. It is available under the [MIT License](LICENSE) and as [`dyro` on PyPI](https://pypi.org/project/dyro/).
