# Changelog

## Unreleased

- Optional `local-image-gen` sidecar discovery: `dyro doctor` reports PATH
  presence only, and `dyro image doctor` / `dyro image install` stay off the
  coding-tool catalog and Skill seats. Dyro still does not generate images.

## 0.7.4 - 2026-08-18

- First-party Skill seats now include `dyro-executor` and `dyro-board` beside
  the existing control-plane and dispatch Skills. Setup and an opted-in
  control plane install or sync the whole first batch. Each seat auto-loads
  from context; loading a seat is not consent to mutate. Opening a task
  prints the executor seat; opening a line prints the navigator seat.
  `dyro-reviewer` and personal skill directories stay out.

## 0.7.3 - 2026-08-18

- Console home no longer treats empty attention as idle. The overview
  says unread, workspace cards say "摘要未列出关注项", and zero-count
  attention chips are not marked success. Ready work still can say
  there is nothing that needs you on the spot.
- Bare `dyro` home prints the workspace alias, not the local root.
  Unhealthy `next` repair findings and commands stay path-free and
  use `--workspace`. Isolated Console no longer allowlists `task next`.
- CI wheel/sdist smoke now asserts `dyro.bridge` is absent, matching
  the publish gate.

## 0.7.2 - 2026-08-17

- `objective explain` and a ready `next` now lead with a three-line
  switch-tool briefing: human matter plus one path-free read command.
  A single live Objective makes `next` emit `tick` or `attention`
  directly. `next.commands` stays empty. Empty attention stays unread,
  not idle. This is not a cross-harness chat resume.
- Console home recommendations now match that follow-up: `tick` when
  ready, `attention` when the user must act. Empty attention still
  opens the workspace, not `task next`.
- Bare `dyro` / `dyro home` prints the same briefing above the tool
  menu so switching harnesses does not start by picking a chat. When
  that briefing exists, Enter defaults to "做下一步" and does not
  launch an editor. `dyro start` still opens a tool, but prints the
  briefing first.
- `objective tick` and `objective attention` text now open with the
  same heading and matter, then a closer without another "下一步".
  Machine SHA lines stay below. JSON envelopes are unchanged.
- Console workspace detail now projects already-captured lines, tasks, and
  objectives from the same summary snapshot. Overview polling stays
  count-only. Summary Proof and task integration stay `not_inspected`;
  `PROOF_DECAYED` stays on the independent inspect.
- Console overview now rolls up `task_status_counts` from readable
  workspaces only. Unreadable workspaces stay unknown and are not counted
  as zero.
- Console workspace detail now lists already-captured Objective attention
  in Chinese. Empty attention stays "摘要未列出关注项"; an unreadable
  workspace stays unknown. Summary still strips `PROOF_DECAYED`.
- Console home now leads with a "现在需要你" list and a human why for
  the next command. Workspace cards show the current matter instead of
  freshness jargon. Empty tools and empty attention still mean unread,
  not "nothing to do".
- Console `GET /api/v1/system` reads the cached update record only.
  `tools` stays empty and `tool_inspection=not_inspected`. The shell
  fetches it on start and manual refresh, not on the 5s overview poll.
  A broken cache is unread and path-free. Empty tools means unprobed,
  not "no tools".
- Console meta advertises `proofs`. The shell fetches
  `GET /api/v1/workspaces/{alias}/proofs` only when that capability is present.
- Overview summary cards now carry `proof_inspection=not_inspected`. Isolated
  inspection rejects an `inspected` summary so the two Proof entries cannot
  collapse.
- Independent Proof inspect stays in the exec worker so a hung git is reaped
  by the parent's process-group kill. Nested spawn is withdrawn: a successful
  worker exit must not leave inspect descendants. Timeout reports unread, not
  inspected.
- Console labels Proof kinds and statuses in Chinese. Overview cards no
  longer treat unread summary as workspace Proof state. `live` is kind-specific
  and is not merge clearance.
- `0.7.x` release gates ignore comment-only markers, refuse `0.8` / `0.9`
  feature numbers, and keep a later `1.0.0` identity tag on the same stranger
  contract. Wheel smoke now runs the stranger bundle check.
- Production `BudgetUsage` / `BudgetRequest` read `trusted_usage` from the
  executor Card. The default is untrusted. Proof still does not feed
  no-progress exhaustion.
- `proof list --line` includes `trigger_observation` from Objectives on that
  line and stays mutually exclusive with `--task` / `--objective`. `--task`
  still excludes triggers.
- Console inspection fails closed on Windows without starting a worker.
- `objective tick` and `objective plan` preview `decide_budget` for the
  selected wave. Automatic Objectives pass `automatic=True`; supervised
  apply stays `automatic=False`. Untrusted usage hard-stops only when
  `workspace.max_provider_usage` is set. Preview never reserves.
- `run_task_bound_dispatch` now requires the Capability plane on write and
  refuses a Card without `execute`. No Card remains the 0.6.9 second door.
- Console meta advertises `surfaces` (and keeps `capabilities` as an alias)
  so HTTP feature flags are not Capability Cards.
- Agent Bridge S1 contracts live in source as `dyro.bridge`. The default
  wheel still omits that package and does not grow `dyro-bridge` /
  `dyro-mcp` scripts.
- Agent Bridge S2/S3 source adapters resolve workspaces, emit path-free
  observations, and return non-executable Objective plans. Catalog marks
  those IDs `implemented_testable`. Authoritative Git observations stay
  unavailable. No transport entry point and no default-wheel package.
- Agent Bridge S4 source transport parses one bounded JSON request and
  emits one redacted response. Public exposure stays empty: hello and
  plans are `implemented_testable` only. No `dyro-bridge` script.
- Agent Bridge S5 source catalog promotes the seven Mandatory operations
  to `public_available` only on Linux. macOS/Windows public exposure
  stays empty. In-process zero-effect traps cover hello. Still no
  console script, wheel package, or CI gate.
- Agent Bridge S6 adds a source-only Skill and `python -m dyro.bridge`
  public process. The Skill is not an integration asset and is not
  installable. Default wheel and `dyro-bridge` / `dyro-mcp` scripts stay
  absent.
- Agent Bridge S7 / `dyro-mcp` stays out. Design status lines now match
  the `0.6.3` removal: published artifacts remain CLI + Skill, not Bridge
  or MCP.

## 0.7.1 - 2026-08-16

- Console opens an independent Proof inspect at
  `GET /api/v1/workspaces/{alias}/proofs`. Summary stays
  `proof_inspection=not_inspected` and does not evaluate Proofs.
- Derive `trigger_observation` from Objective trigger files using
  `next_probe_at` only. Expired probes are display-decayed and stay out of
  `progress_fingerprint` and merge kinds.
- Every `0.7.x` release gate now requires the inspect and trigger markers and
  still refuses to narrate the train as `1.0.0`.

## 0.7.0 - 2026-08-16

- First delivery-physics release: Proof objects, Capability Cards, the Host
  Compiler, and `verify-bundle`. This is not the published `0.6.9` Peer Wave
  train and is not `1.0.0`.
- A Card without `execute` refuses write dispatch, Peer Wave binding, and
  `task run`. PATH discovery is not a Card. A dispatch-ready provider without a
  Card remains the explicit second write door from 0.6.9.
- Core bound contracts no longer silently set `allow_unconfined_provider`.
- `merge_task` still refuses missing review or signoff files, but only a failed
  complete predicate is labeled `PROOF_DECAYED`.
- Console summary stays `proof_inspection=not_inspected`; `dyro objective
  attention` is the Proof-rebinding entry.

## 0.6.9 - 2026-08-15

- Make multi-harness writes a Core peer wave: each task worktree has one
  executor, overlapping slices share a `conflict_group`, and wave members are
  not live supervisors. Objective budgets now default to `max_parallel = 3`;
  when write-capable Providers are ready the effective wave size is
  `min(requested, ready_count)`. Empty `conflict_group` values warn in a
  parallel wave. `task.executor` can run a ready dispatch adapter inside the
  existing task worktree (Cursor edit stays fail-closed); scratch
  `dispatch run --mode edit` remains detached and patch-only. `objective tick`
  and `task daemon` preview idle-harness bindings and honor per-backend caps.

## 0.6.8 - 2026-08-15

- Fix Console inspection falsely timing out healthy workspaces by budgeting for
  isolated Python process startup. Unavailable or missing workspace counts now
  render as unknown instead of zero, and partial pages no longer claim that all
  projects are healthy.
- Add executable dispatch adapters for Cursor Agent, OpenCode, Grok, Hermes,
  Kimi, DeepSeek Harness (`dsh`), and Pi alongside Codex and Claude. Every
  adapter now has bounded process supervision, an explicit read/edit tool or
  sandbox policy, a backend-specific authentication probe, and a structured
  result decoder. Cursor dispatch intentionally requires `CURSOR_API_KEY` so
  it can run with an isolated home instead of loading user MCP processes;
  Cursor edit dispatch remains fail-closed until its sandbox process lifecycle
  can be proven. Provider credentials are scoped to the selected model, and
  successful-looking CLIs cannot leave closed-stdio descendants running.
  Hermes receives only projected task context: user rules, identity, memory,
  fallback providers, background review, and session persistence are disabled.
  Pi requires Node.js 22.19.0 or newer.
- Preserve a multi-harness panel board when one member fails instead of losing
  the other members' terminal results; deduplicate and cap explicit members,
  and let every ready integrated Provider participate in selection. Default
  panels remain a cost-bounded three-Provider sample; explicit `--members all`
  runs every ready Provider with at most four concurrent members.
- Add the separately managed `dyro-dispatch` Skill for explicit parallel,
  delegated, and independent-agent work. It installs through
  `dyro integration install dispatch`, keeps outbound Provider effects separate
  from the read-only control plane, and preserves all gate, signoff, merge, and
  push boundaries. A single setup opt-in now installs both first-party Skills;
  existing managed control-plane installs automatically gain Dispatch, and both
  stay synchronized across interactive launches and package updates.
- Add persistent Batch V1 orchestration for two to four heterogeneous roles:
  side-effect-free planning, digest-bound idempotent start, compact status,
  bounded partial-result recovery, and cooperative cancellation. Plans bind
  Provider choices, guarded context, timeouts, and edit HEAD; all members pass
  preflight before any Provider starts, at most one may edit, and cleanup that
  cannot be proven remains visible instead of being reported as cancelled.
  Workers revalidate planned context immediately before Provider use, and edit
  worktrees are pinned to the reviewed object ID.

## 0.6.7 - 2026-08-13

- Make bare `dyro update` check, confirm, and install (same path as
  `update now`). `update check` remains check-only; `--yes` skips the
  confirmation on both `update` and `update now`.

## 0.6.6 - 2026-08-13

- Launch any installed coding tool from `dyro start` / `open`, not only a
  Codex Profile adapter; `agent add --preset` now covers the catalogued
  launchable tools.
- Treat a healthy workspace as ready with no mutation: `next --format json`
  no longer hands Agents a `start --agent codex` command.
- Install the control-plane Skill avatar for Grok when `~/.grok` is present.
- Teach the packaged Skill to read `objective explain --format json`.

## 0.6.5 - 2026-08-13

- Expand the packaged `dyro-control-plane` Skill into a host-neutral,
  intent-routed read-only control surface for workspace health, lines, Change
  Sets, integrations, and Objective attention/graph/tick/plan observations.
- Add stable JSON views for `workspace list`, `status`, `doctor`, `next`,
  `line list`, `changeset list|verify`, `integration status`, and
  `objective list|status` while preserving existing text output by default.
- Make `objective list` and `objective status` strictly zero-write by refusing
  to recover an interrupted Objective transaction during observation.
- Run control-plane Git observations with `git --no-optional-locks` so status,
  doctor, and Change Set verification cannot refresh Git index metadata.
- Bind workspace-local `next --format json` handoffs to their resolved alias or
  absolute root, and only offer bootstrap when every failure is an absent
  repository with a configured remote.
- Return one stable JSON error envelope for machine-facing runtime failures and
  use deadline-, byte-, record-, and symlink-bounded reads for Profile, line,
  Change Set, integration, Objective, Task, evidence, and Git observations.
- Keep machine-facing Objective completion consistent with text views by
  checking Task integration evidence and Git ancestry inside the same budget;
  reject unsafe bootstrap targets before `next` can hand off a mutation.
- Minimize Agent-visible local metadata: workspace and Skill integration JSON
  and health diagnostics omit absolute paths by default, expose them only
  through explicit `--include-paths`, and let the Skill skip global discovery
  when an alias is already known.
- Let Enter confirm the already-previewed feature worktree plan while keeping
  `b` as the explicit route back to baseline selection.
- Exclude generated Python bytecode from source and wheel distributions, even
  when release tests imported packaged integration assets before the build.

## 0.6.4 - 2026-08-12

- Guide control-plane Skill install during interactive `dyro setup` (preview in
  the plan; apply only after confirmation; soft-fail if no host is ready).
- After a successful package update (`dyro update now` or patch auto-update),
  best-effort sync an already-managed Skill through the fresh `dyro` entry
  point (`dyro integration sync skill --yes`).
- On interactive `dyro` / `home` / `start` launches, automatically repair an
  outdated managed Skill; never first-install on startup.
- Make bare `dyro update` equivalent to `dyro update check` (common CLI
  ergonomics; `check` remains as an explicit alias).
- After a same-turn package auto-update Skill refresh, skip in-process startup
  Skill sync so stale in-memory assets cannot overwrite the fresh write.
- Pin post-update Skill sync to this install (`bin/dyro` beside the interpreter,
  else `python -m dyro`) instead of bare `PATH` lookup.
- Make setup Skill UX honest when no agent host is present (defer by default;
  completion reports install outcome).

## 0.6.3 - 2026-08-12

- Keep the shipping surface as CLI + Skill only. PyPI releases through 0.6.2
  never exposed `dyro-bridge` / `dyro-mcp` entry points or the optional `[mcp]`
  extra; those remain out of the wheel. Historical ADR/design/evidence docs stay
  in the repository as archive only. Upgraders from interim git builds that had
  those entry points should expect them to be absent after upgrading.
- Install the cross-platform Skill as a Dyro-owned **mirror** under
  `DYRO_HOME/skills/dyro-control-plane`, with per-host **avatars** (symlinks /
  Windows junctions) for detected agent homes (Codex, Claude, Agents, Cursor).
  After upgrading, preview then install with
  `dyro integration install skill --dry-run` / `dyro integration install skill --yes`
  (or the `codex` alias). The Skill uses read-only `dyro` CLI commands
  (`workspace list` / `status`, `objective list|status|plan`).
- Migrate legacy whole-directory Codex Skill installs to mirror+avatar on the
  next owned install, but only when the legacy target is a detected host avatar
  whose content matches the packaged Skill assets (fail closed otherwise).
- Let interactive line/hotfix repository picks accept list indices and/or
  repository IDs; when a token matches a repository ID exactly (including
  pure-numeric IDs), the ID wins over index interpretation.

## 0.6.2 - 2026-08-05

- Extend interactive `dyro setup` and Profile onboarding with a single,
  preview-first personal-preferences step: update checks, optional patch-only
  auto-updates, a locally detected coding-tool default, and Console project
  selection can be saved together, while cancel and dry-run remain read-only.
- Make coding-tool choice easier to scan by showing a short detected list
  first, exposing the full catalog on demand, and accepting a supported tool
  identifier directly even when it is not in the initial shortlist.

## 0.6.1 - 2026-08-04

- Make the global Dyro home, first-run setup, line creation, and Hotfix
  creation genuinely guided: show safe previews, offer meaningful defaults,
  support retry/back/cancel, and preflight every selected repository before
  any Git worktree mutation.
- Register setup Profiles with the global Console by default while preserving
  an existing default workspace, and make workspace, repository, Agent, and
  coding-tool management more readable and actionable.
- Expand the coding-tool catalog across CLI and desktop launchers, including
  Codex, Claude, Antigravity, ZCode, and Qoder-family tooling; unavailable
  tools remain informative rather than blocking a workspace launch.
- Refresh the read-only Console into a compact Signal Room command center and
  align terminal output with semantic colors, clear status grouping, and
  graceful interruption recovery.

## 0.6.0 - 2026-08-04

- Add the native, local-first Continuation engine: versioned Objectives have
  explicit ownership, deterministic planning, bounded scheduling waves,
  budgets, triggers, attention projection, action journals, and supervised
  execution. It remains opt-in and explicitly confirmed, uses the existing
  Task execution and review APIs, and never bypasses the established review,
  sign-off, merge, or push boundaries.
- Add `dyro console`, a read-only local Web Console for registered workspaces.
  It binds only to loopback, exchanges a one-time browser fragment secret for a
  tab-scoped session, and shows health, attention, Task status, active
  Objectives, and safe next CLI commands without browser-initiated Core or
  project mutations and without external service access.
- Package the Console's fixed static resources in both wheel and sdist,
  validate their digest manifest before listening, and verify those resources
  from clean installed artifacts in CI and the release workflow.
- Harden execution identity, terminology policy scanning, signed evidence, and
  control-state handling for the new supervised surfaces.

## 0.5.6 - 2026-08-03

- Check the official PyPI endpoint at most once per local day on interactive
  home launches, cache the result in user-level Dyro state, and never block
  workspace entry when the network or state directory is unavailable.
- Add `dyro update check|now|enable|disable` and opt-in patch-only automatic
  updates with installer-aware, shell-free commands and post-update version
  verification; minor and major updates still require confirmation.

## 0.5.5 - 2026-08-03

- Add an availability-first coding-tool catalog with per-workspace history,
  non-binding project recommendations, and local default/pinned preferences.
- Detect Cursor Desktop separately from Cursor CLI and support OpenClaw as a
  workspace-scoped external runtime without granting Dyro delivery authority.
- Guide confirmed installation of missing tools with built-in shell-free argv,
  official-page fallback for remote scripts, and post-install version checks.

## 0.5.4 - 2026-08-03

- Add a zero-friction global `dyro` home that registers workspaces and lets
  users resume a recent workspace, development line, or task from any
  directory.
- Keep explicit workspace and Agent commands available for scripts, and make
  stale or unhealthy entries actionable without blocking healthy workspaces.
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
