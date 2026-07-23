# Changelog

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
