# Changelog

## Unreleased

- Add a generic, team-owned workspace-blueprint contract with
  `dyro blueprint validate` and preview-first `dyro join` onboarding.
- Pin every repository in a joined development line to an immutable full
  commit SHA, keep anchors detached, and create isolated linked worktrees.
- Make joins resumable without overwriting unrelated targets, reject embedded
  credentials and symbolic-link redirects, and keep organization-specific
  repository details outside Dyro Core.
- Always show the interactive coding-tool picker before opening a line or task,
  and allow supported locally detected tools to open the workspace without
  granting them Dyro execution, gate, review, merge, or push authority.

## 0.5.3 - 2026-08-01

- Add a preview-first interactive `dyro setup` path, a read-only `dyro next`
  guide, and an explicit non-interactive mode for scripts and CI.
- Keep control state out of a Git project root by proposing a sibling
  workspace, preserve the source branch as the suggested base, and never
  register a detected Provider without an audited Core adapter contract.
- Synchronize newcomer guidance across all README translations, make Dyro's
  own test command discoverable, and prevent released-version changelog drift.

## 0.5.2 - 2026-08-01

- Close dispatch secret boundaries for task text and Provider results; real
  Providers now require explicit acknowledgement and read-only runs use a
  guarded context projection.
- Make `echo` an explicit offline simulation, strengthen dry-run validation,
  and expose discovery-only local Provider commands without treating them as
  executable adapters.
- Prevent public task-status changes from bypassing review, revalidate review
  evidence at merge, recover failed attempts to retryable state, and keep
  external QUESTION continuations on a monotonic provenance lineage.
- Verify trusted release-tag ancestry, lock the release build environment, and
  document the incident/yank response path before the next publication.

## 0.5.1 - 2026-07-31 (superseded)

> The Git tag and GitHub Release remain the historical record. The 0.5.1
> distribution is no longer available from PyPI; install or upgrade to 0.5.2
> or later.

- Remove the Docker-backed external semantic runtime from the default package,
  CLI, release validation, and CI; preserve its source, tests, and design
  history in the separate semantic-runtime archive.
- Remove the dispatch `stage5-bridge` so local provider dispatch has no runtime
  dependency on the removed Docker experiment.
- Run local dispatch edits in detached Git worktrees with hash-bound patches; make default dispatch truly asynchronous.
- Add atomic run claims and owner-token leases, bounded process-tree execution, sealed context reads, strict JSON results, safe GC, authenticated backend routing, and real parallel panels.
- Reject real CLI adapters in `strict` mode unless they can prove OS-level isolation; Codex/Claude permission profiles are no longer described as physical isolation.
- Route top-level `--root` / `--dry-run` safely into the dispatch surface and
  align ADR/design documentation with shipped behavior.

## 0.5.0 - 2026-07-30

- Ship first-party `experiments.*` modules in the `dyro` wheel (local agent dispatch L0–L4, external workflow runner Stage0–5).
- Add `dyro dispatch …` for local multi-agent dispatch (advisory only; never merge/push/signoff).
- Add `dyro runtime status|production-gate` for external semantic runtime status; production remains **NOT_READY**.
- Align ADRs, architecture, and multi-language READMEs with the packaging decision; embed Mermaid diagrams in READMEs (no tracked PNGs).
- Add dispatch boundary tests and CI wheel-install smoke for `dispatch` / `runtime`.
- Close the 2026-07-30 adversarial review board with Conditional Go (Final Arbitration).

## 0.4.1 - 2026-07-24

- Roll back partially created development-line worktrees when a later repository fails during `line create` / `hotfix create`.
- Serialize task merges into the same delivery line with a per-line merge lock (covers manual and auto-merge).
- Align `task daemon` with `task loop`: dispatch `backlog` and `assigned` tasks, honor dependencies/conflict groups via `check_dispatchable`.
- Read the package version from installed distribution metadata so `pyproject.toml` is the single source of truth.

## 0.4.0 - 2026-07-23

- Add `dyro setup` for one-command Profile discovery, state-directory setup, and an explicitly confirmed first development line.
- Add safe `config get/set` and `agent add/test` commands so common Profile and adapter changes do not require hand-editing TOML.
- Add portable external execution evidence bundles that run declared gates, bind clean task HEADs, reject unsafe ZIPs, and import through the existing evidence contract.
- Make Profile, line, Change Set, task-state, evidence, sign-off, and ledger writes atomic or lock-protected; task claims and state transitions now serialize across Dyro processes.
- Use deterministic, path-safe gate-log filenames and add Ruff to pull-request CI.

## 0.3.0 - 2026-07-23

- Fail closed on invalid TOML booleans instead of treating non-empty strings as enabled policy.
- Bind reviews and external sign-off to both the execution receipt and an exact per-repository task HEAD snapshot.
- Reject dirty, drifted, stale, or foreign task worktrees and detect reviewer source mutations.
- Enforce clean delivery-line worktrees as a non-optional transactional merge invariant.
- Preflight every repository before merging, roll back staged local merges on failure, and defer push until all local merges succeed.
- Make Change Set verification reject dirty delivery-line worktrees.
- Add pull-request CI, pin release Actions to immutable commits, and reduce release-artifact retention.

## 0.2.1 - 2026-07-23

- Establish a clean public Git root for the current DyroEngineeringFlow source snapshot; no functional changes from 0.2.0.

## 0.2.0 - 2026-07-23

- Generalize the control plane: remove project-specific public migration material and document the reusable Core/Profile boundary.
- Add per-repository base refs and declared `linked-worktree` / `anchor-reference` storage modes, with topology and branch checks in `doctor`.
- Add external-runner mode with one-time task claims, receipt-bound gate logs, evidence import, receipt-bound reviews, and optional external sign-off.
- Add cross-repository Change Sets that pin and verify clean delivery-line Git heads.
- Prevent repository discovery from importing version, task, or hotfix worktrees as duplicate anchors.

## 0.1.1 - 2026-07-23

- Add `dyro init --discover` to generate a Profile by scanning local Git repositories and their `origin` remotes.
- Add `dyro repo add` and `dyro repo list` so repository anchors can be managed without manually editing `dyro.toml`.
- Reject unsafe repository mount paths before writing a Profile.

## 0.1.0 - 2026-07-23

- Initial standalone DyroEngineeringFlow product and `dyro` CLI.
- Dynamic multi-repository workspaces, functional release lines and explicit-base Hotfixes.
- Agent adapters, task worktrees, decision gates, independent review, guarded merge and append-only ledger.
- Add MIT licensing, PyPI metadata, and trusted GitHub Actions publishing preparation.
