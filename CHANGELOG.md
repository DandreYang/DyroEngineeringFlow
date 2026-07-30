# Changelog

## 0.5.1 - Unreleased

- Add a create-only production-acceptance operator kit that locates packaged
  schemas, stably hashes real distributions/SBOM/provenance/providers/operations,
  prepares explicit unsigned records, exports exact HSM signing bytes, and
  verifies externally produced signatures without loading production private
  keys or granting deployment authority.
- Add auditable `checked_at`, non-looping next commands, installed-artifact
  smoke coverage, an operator runbook, and adversarial overwrite/link/FIFO/
  replacement/signature tests for the production acceptance journey.
- Add a fail-closed production acceptance path: one release-bound manifest and
  three expiring, purpose-separated environment attestations must verify under
  four distinct trusted Ed25519 public keys before `PROD-01/02/09` can clear.
- Ship strict deployment-manifest and production-attestation schemas; reject
  tampering, weak pass assertions, role mismatch, key reuse, expiry, and
  cross-release/environment drift while retaining independent release approval.
- Add operator-friendly `dyro runtime status/doctor/plan`, make a closed production gate return exit code 3, and preserve JSON output for automation.
- Bind Stage5 leases to exported Dyro Core claim authority; verify sealed-pack identity and live workspace artifacts before building a signed Core execution bundle.
- Add an end-to-end Stage5 pack → Core import → independent review test while keeping import/review/signoff/merge/push outside runtime authority.
- Publish claim and evidence outputs without following dangling symlinks or overwriting concurrent files; distinguish dry-run, gate-blocked diagnostic bundles, and import-ready handoffs.
- Verify Stage0 cleanup by ownership label plus exact container ID across a bounded Docker daemon settle window.
- Update CI and release artifact smoke tests to require the intentional `NOT_READY` exit code 3 instead of treating a closed production gate as success.
- Restore the TypeScript semantic runtime and Stage1–5 bundle sources in both wheel and sdist; release CI now installs and assembles Stage1/Stage5 from each artifact outside the checkout.
- Make external claim renewal compare-and-swap exact owner generations, reject unsafe claim files, and aggregate Supervisor shutdown checks.
- Prove Docker container and network cleanup fail-closed, including partial startup and readiness failures.
- Bind evidence files to the validated result envelope, seal manifest/ZIP consistency, reject unsafe or oversized pack members, and avoid partial packs.
- Run local dispatch edits in detached Git worktrees with hash-bound patches; make default dispatch truly asynchronous.
- Add atomic run claims and owner-token leases, bounded process-tree execution, sealed context reads, strict JSON results, safe GC, authenticated backend routing, and real parallel panels.
- Reject real CLI adapters in `strict` mode unless they can prove OS-level isolation; Codex/Claude permission profiles are no longer described as physical isolation.
- Route top-level `--root` / `--dry-run` safely into experiment surfaces and align ADR/design documentation with shipped behavior.

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
