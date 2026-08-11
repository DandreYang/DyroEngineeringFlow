# Dyro Agent Bridge Phase 0 Local Fix — Adversarial Review Board

Date: 2026-08-10 (Asia/Taipei)

Scope:
- Repo: `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev/dyroengineeringflow`
- Branch: `feat/dev` @ `e284c1ce2da731c404ab3866124026a28d03691c`
- Mode: code review of uncommitted local-fix WIP + local Docker audit evidence claims

Reviewed Materials:
- Uncommitted fix diff (5 files):
  - `.github/workflows/ci.yml`
  - `tests/fixtures/bridge/Dockerfile.audit`
  - `tests/test_bridge_strace_audit.py`
  - `tests/test_release_source.py`
  - `tools/verify_bridge_zero_effects.py`
- Handoff: `plans/dyro-agent-bridge-cursor-handoff-2026-08-10.md`
- Local verification claims from Cursor wrap-up session (2026-08-10): source/wheel/sdist six reports PASS; package/contract digests match
- Acceptance SSOT: `docs/designs/agent-bridge-phase-0-acceptance.md`
- Control-plane skill: `src/dyro/integrations/assets/dyro-control-plane/SKILL.md`

SSOT:
- `docs/designs/agent-bridge-phase-0-acceptance.md`
- `plans/dyro-agent-bridge-cursor-handoff-2026-08-10.md`
- Live source + uncommitted fix diff above

Out of scope for this board (do not reopen unless source proves wrong):
- Redesigning Agent Bridge Phase 0 architecture
- Treating user WIP `plans/dyro-agent-bridge-phase-0.md` as part of this fix commit
- Calling/simulating `dyro dispatch`, objective apply, merge, push, release, publish
- Blindly relaxing CI timeouts without Ubuntu runner evidence

## Rules

1. Each reviewer writes only in their own signed section.
2. Conflicts are resolved by source code, live contracts, or retained evidence artifacts.
3. Unprovable claims are marked `须人工核`.
4. Findings use P0/P1/P2 severity.
5. Code review mode: bugs, regressions, security, broken contracts, missing tests first.
6. Local Docker evidence is not exact-commit Ubuntu CI evidence.
7. Do not edit, rewrite, or summarize another reviewer section.

## Fixed Decisions

- Phase 0 public Bridge availability remains Ubuntu 24.04 only; macOS/Windows stay fail-closed.
- Zero-effect / Landlock / tool-list / fail-closed assertions must not be weakened to make tests green.
- Existing Docker images, evidence volumes, and `/private/tmp` audit contexts must be retained.
- Commit / push / PR require separate explicit user authorization.

## Open Micro-Decisions

1. Should CI `bridge-zero-effects` timeouts be changed before the first real Ubuntu PR run, or only after timeout failure evidence?
2. Should the untracked handoff markdown be included in the fix commit, kept untracked, or moved under `docs/superpowers/reviews/`?
3. Are the six local Docker reports sufficient to call “本地修复完成”, while Phase 0 formal Go remains blocked on F01–F04 + exact-commit CI + this board?

---

# Code Contract Reviewer Review Section

Reviewer: Code-Contract-Agent
Time: 2026-08-10 22:05 Asia/Taipei
Verdict: Conditional Go (merge this local-fix commit only)

## Findings (severity-ordered)

### P1 — Stale `git am` session blocks safe commit of this fix
- Evidence: `git status` reports “You are in the middle of an am session”; worktree gitdir `.../worktrees/dyroengineeringflow/rebase-apply/` holds patch `0001` for already-landed `e7e1225` (dated 2026-08-07), with `next=1` / `last=1`. Fix files have no conflict markers.
- Contract impact: any commit/`am --continue` on this worktree risks mixing unrelated patch state with the five-file fix.
- Fix: `git am --abort` (or equivalent cleanup) **before** staging; then stage only the five fix paths. 须人工核 that abort does not discard intended WIP outside those paths.

### P1 — Local audit reports assert `dirty=clean` while harness ≠ HEAD
- Evidence: all six `/private/tmp/dyro-bridge-reports.ywZ3zl/{source,wheel,sdist}-{candidate,public}-report.json` have `passed=true`, 43 ops, 11 unavailable@exit4, `trace.ok=true`, public `binder=2` / `landlock_success=2`, shared `contract_digest=sha256:2769249643ca1e03738d0f175c121dd879230ee8740a8fc65f413957c511971e`, shared `package_manifest_sha256=sha256:baaf9c710d7a32dd332da0987a93a1073e8904e8d7de0d0142d7b118ca25a70e`, `commit=e284c1ce2da731c404ab3866124026a28d03691c`, `dirty=clean`.
- Counter-evidence: live `tools/verify_bridge_zero_effects.py` sha256 `7c6057b833efe6813afb904ab9d9de1368b88a658fe9dfa937d996d867bfd2eb` matches report `harness.verifier_sha256`; `git show HEAD:tools/verify_bridge_zero_effects.py` hashes to `746f86725f5acc98832bc02ac07043121f1dfa8da1cf9aaf30773a24067e8a7d` (different). `--dirty` is CLI/env asserted (`verify_bridge_zero_effects.py` ~1305 / CI `DYRO_AUDIT_DIRTY=clean`), not measured from git.
- Contract impact: results are valid **local repair-candidate** evidence (dirty harness + clean package@HEAD), **not** exact-commit / release evidence. Do not promote `dirty=clean` wording to formal Go.

### P1 — Residual CI wall-clock risk (pre-existing; not introduced by this diff)
- Evidence: `.github/workflows/ci.yml` `bridge-zero-effects` has `timeout-minutes: 10` (L55) while each of three serial `docker run` invocations allows `timeout 8m` (L118); handoff §5.3 recorded ~9 minutes for **one** source public+candidate path on Colima.
- Contract impact: this fix does not change timeouts; first Ubuntu PR may still fail on job budget even if the three semantic fixes are correct. Per board rule / open micro-decision #1: **do not** widen timeouts in this commit without Ubuntu failure evidence. 须人工核 after first exact-commit CI run.

### P2 — Regression tests are string/shape guards, not full Docker rebuilds
- `tests/test_release_source.py` L99–114 and `tests/test_bridge_strace_audit.py` L201–206 assert workflow/Dockerfile text; `test_objective_plan_fixture_uses_the_existing_anchor_repository` (L123–128) asserts `storage_for("api")=="anchor-reference"` only. Acceptable as unit regression for this fix; black-box still owned by Ubuntu Docker gate.

## Contract Consistency

Cross-module contracts for the three root failures are aligned and do not weaken zero-effect / Landlock / tool-list / fail-closed gates:

| Failure | CI | Dockerfile | Fixture / verifier | Tests |
|---|---|---|---|---|
| hash-locked + unpinned build tools | Two `pip download` calls (`.github/workflows/ci.yml` L83–86); `uv export` requirements are hashed (tmp context sample) while `setuptools`/`wheel` absent from that file | Offline install still `--no-index --find-links=/audit/wheelhouse` | N/A | `test_ci_downloads_hash_locked_runtime_and_build_tools_separately` requires both snippets |
| `groupadd`/`useradd` not on PATH | Copies working-tree `Dockerfile.audit` into audit context (L90) | Runtime `PATH=/audit/venv/bin:/usr/bin:/bin` (L52) + absolute `/usr/sbin/groupadd|useradd` (L59–60); PATH not widened | N/A | Runtime-stage asserts absolute `/usr/sbin/...` (L204–206) |
| `objective.plan` early `RECORD_INVALID` | Harness script copied from tree (L92) | CMD runs candidate then public verifier | `prepare_fixture` writes `[storage_modes] api = "anchor-reference"` (`verify_bridge_zero_effects.py` L177–179); matches `Line.storage_for` default `linked-worktree` (`workspace.py` L32–33) and plan path selection (`plans.py` L511–515) | New fixture unit test L123–128 |

No redesign; gates that require `binder==2` / `landlock_success==2` remain; fixture change **enables** those proofs instead of failing closed before Git bind.

## Source Evidence Accuracy

- **Proven:** six exported reports PASS with digest/parity claims above; volumes `dyro-bridge-evidence-source-r3-*`, `wheel-r4-*`, `sdist-r3-*` exist alongside older diagnostic volumes.
- **Proven limitation:** package artifact @ HEAD `e284c1c` + dirty harness (verifier digest mismatch) ⇒ local candidate only (handoff §5.5). Matches board rule “Local Docker != exact-commit Ubuntu CI”.
- **须人工核:** whether any retained volume/`DYRO_AUDIT_DIRTY=clean` run was accidentally reused after further tree drift beyond the five reviewed files; current worktree also has out-of-scope `M plans/dyro-agent-bridge-phase-0.md`.

## Decision Validity

1. **Timeouts:** keep current budgets until Ubuntu failure evidence — agree with open micro-decision #1; do not bake speculative timeout edits into this fix.
2. **Handoff markdown:** not required for the code contract of the five-file fix; include only if the commit message/docs policy wants operator SSOT. Keep `plans/dyro-agent-bridge-phase-0.md` out of the fix commit (fixed decision).
3. **“本地修复完成”:** acceptable as **local repair-candidate verification complete**; unacceptable as Phase 0 formal Go (F01–F04 + exact-commit CI still open).

## Plan Executability

Merge path for **this fix** is executable after process hygiene:

1. Abort stale `git am`.
2. Commit only: `ci.yml`, `Dockerfile.audit`, `test_bridge_strace_audit.py`, `test_release_source.py`, `verify_bridge_zero_effects.py`.
3. Push/PR under separate user auth; treat first Ubuntu `bridge-zero-effects` as the real integration proof.
4. Do not treat `/private/tmp/dyro-bridge-reports.ywZ3zl` as release evidence artifact.

## Scope And Risk

- Scope of the five-file diff is tightly matched to the three diagnosed failures; no acceptance-matrix weakening observed.
- Main residual risks: stale am session; CI wall-clock; mis-promotion of dirty-harness local reports; accidental inclusion of user WIP plan file.

## Go/No-Go

**Conditional Go** for merging **this local-fix** (not Phase 0 release Go).

Conditions: clear `git am`; exclude `plans/dyro-agent-bridge-phase-0.md`; no timeout weakening in this commit; language stays “local candidate fix”, not exact-commit/release.

## Required Fixes

1. **[P1/process]** Resolve stale `git am` before any commit of these paths.
2. **[P1/scope]** Stage only the five fix files; leave user WIP plan unstaged.
3. **[P1/claims]** When recording completion, state harness dirty vs package@HEAD; do not cite these six reports as `dirty=clean` exact-commit evidence.
4. **[P1/follow-up, not in this commit]** After first Ubuntu CI result: if job hits 10m / container 8m, then adjust timeouts with that evidence (open micro-decision #1).

---


# Security Reviewer Review Section

Reviewer: Security-Agent
Time: 2026-08-10 ~21:55 Asia/Taipei
Verdict: **GO for merging this fix patch** (security intent preserved). **NO-GO for Phase 0 formal release** (unchanged blockers: F01–F04, exact-commit Ubuntu CI, dirty harness ≠ release evidence).

Risk Level (this fix patch): **LOW**
Finding counts: P0=0, P1=0, P2=4, 须人工核=2

Adversarial focus: PATH expansion temptation, hash-lock bypass, fixture `storage_mode` capability lying, false Landlock evidence, claim inflation of local audits to formal Go.

## Contract Consistency

Security gates in acceptance SSOT (B01–B05 Landlock/zero-effect, fail-closed public Bridge on non-Ubuntu, hash-locked offline wheelhouse) remain intact in the five fix files:

1. **PATH / isolation** — Runtime `PATH=/audit/venv/bin:/usr/bin:/bin` is unchanged. Fix uses absolute `/usr/sbin/groupadd` and `/usr/sbin/useradd` at image *build* time only (`Dockerfile.audit` runtime stage). Does **not** widen runtime PATH to `/usr/sbin`. Non-root `USER 10001:10001` and CI `docker run` flags (`--network=none --read-only --cap-drop=ALL --cap-add=SYS_PTRACE --security-opt no-new-privileges=true`) unchanged.
2. **Hash-lock** — Split `pip download` keeps hashed `uv export --locked` requirements on their own command (pip auto-enables require-hashes when `--hash=` lines are present; live export shows 264 hash lines; `setuptools`/`wheel` are **not** in that export). Second download is only unpinned build tools into the same wheelhouse — restores the previously failing intended design; does **not** strip hashes from runtime deps.
3. **Fixture storage_mode** — `prepare_fixture` still only creates `workspace/repositories/api` (no `versions/...` worktree). Declaring `api = "anchor-reference"` matches `Line.storage_for` → `repository_path` in `plans._integration_state`, so `objective.plan` reaches descriptor-binder + Landlock instead of failing early as `RECORD_INVALID`. This is fixture honesty, not a capability widening of Bridge.
4. **Fail-closed** — No relaxation of unavailable-ops (==11, exit 4), Landlock summary asserts (`binder == 2`, `landlock_success == 2`), mutation/network/write_open gates, or macOS/Windows public availability.

## Source Evidence Accuracy

| Claim | Source verdict |
| --- | --- |
| Absolute `/usr/sbin` avoids PATH widen | **Proven** — `git diff` on `Dockerfile.audit`; ENV PATH still excludes `/usr/sbin` |
| Split download preserves runtime hashes | **Proven** — `ci.yml` + live `uv export` hash lines; setuptools/wheel absent from export |
| Fixture uses real anchor path for plan/Landlock | **Proven** — `verify_bridge_zero_effects.py` + `plans.py:511-515` path selection; same `git_reader` / Landlock helper |
| Six local reports PASS with landlock_success=2, mutation=0 | **Proven for artifacts under** `/private/tmp/dyro-bridge-reports.ywZ3zl` (all six; `evidence.commit=e284c1c…`, `dirty=clean`, matching contract/package digests) |
| Those reports are exact-commit Ubuntu CI / release evidence | **False if claimed** — harness includes uncommitted WIP; local Colima/Docker ≠ GHA Ubuntu runner; handoff correctly labels “本地修复候选证据” |
| Full CVE dependency audit clean | **须人工核** — `uvx pip-audit` aborted (`ensurepip` SIGABRT) in this environment; fix patch does not change lockfile/dep pins |

Secrets scan on the five fix files: no hardcoded keys/passwords/tokens.

## Decision Validity

| Fix | Weakens isolation / Landlock / fail-closed / hash-lock / side effects? | Decision |
| --- | --- | --- |
| `/usr/sbin/*` absolute admin tools | No — correct least-privilege alternative to expanding PATH | **Valid** |
| Separate pip downloads | No hash-lock bypass of runtime; residual unpinned build tools pre-existed as intent | **Valid** |
| `anchor-reference` on alpha | No — aligns config with created tree; enables real binder/Landlock evidence rather than fake early failure | **Valid** |
| Regression tests (sbin paths, two downloads, storage_mode) | Strengthen contracts; do not relax asserts | **Valid** |

False-Landlock concern: rejected. Early `RECORD_INVALID` prevented binder execution; after fix, reports show `binder=2` / `landlock_success=2` / `mutation=0` / `network=0` / `write_open=0` via the same `git_read` Landlock ABI≥3 helper. Not synthetic counters alone.

## Plan Executability

- Fix patch is mergeable from a security-regression standpoint.
- Residual executability risks (timeouts 8m/10m, wheel/sdist re-run on exact commit CI) are operational, not security weakenings — do not block *this* patch on security grounds.
- Do not treat local six-report folder as Phase 0 formal Go evidence.

## Scope And Risk

- Scope of security-relevant WIP is correctly limited to CI wheelhouse fetch, audit Dockerfile admin paths, fixture storage_mode, and contract tests. User WIP `plans/dyro-agent-bridge-phase-0.md` is out of this security verdict for the fix commit.
- No Bridge production authn/authz surface changed; no dispatch/apply/side-effect paths introduced.
- Overall risk for **merging the fix**: LOW. Overall risk if **inflating local evidence to release Go**: HIGH (process), not a defect in the patch itself.

## Go/No-Go

- **Merge this fix patch (security):** GO
- **Phase 0 formal release / publish:** NO-GO until F01–F04 + committed exact-SHA Ubuntu `bridge-zero-effects` evidence artifact; local Docker PASS must not be marketed as that gate.

## Required Fixes

None P0/P1 blocking merge of this patch.

### P2 (should harden soon; not merge-blockers)

1. **Regression gap — PATH must stay narrow** (`tests/test_bridge_strace_audit.py:201+`)  
   Assert runtime stage still contains `PATH=/audit/venv/bin:/usr/bin:/bin` and does **not** add `/usr/sbin` to PATH (prevents future “just expand PATH” regressions).

2. **Regression gap — hash-lock semantics** (`tests/test_release_source.py`)  
   Assert first download remains `--requirement` alone (no unpinned packages on that line) and second download is separate; optionally assert workflow still uses `uv export --locked` producing hashed requirements.

3. **Residual supply chain — unpinned build tools in shared wheelhouse** (`.github/workflows/ci.yml:85-86`)  
   `setuptools>=77.0.3` / `wheel` downloaded without pins/hashes into the same `--find-links` store used by offline `pip install` (esp. sdist build). Intentional and not a runtime hash bypass, but pin+hash or isolate build-tool wheelhouse later.

4. **Coverage residual — only `anchor-reference` exercised** (`tools/verify_bridge_zero_effects.py`)  
   Zero-effect Landlock proof path no longer covers `linked-worktree` destination resolution. Not a lie about capabilities; track as follow-up corpus/fixture coverage.

### 须人工核

1. Re-run `pip-audit` (or equivalent) against locked export on a healthy runner — not completed here.
2. Confirm provenance of `/private/tmp/dyro-bridge-reports.ywZ3zl` against the exact WIP harness image digests before any internal “本地修复完成” claim beyond handoff’s candidate wording.

## OWASP / Checklist (scoped to this patch)

- A01 Access control: N/A change (gates unchanged)
- A02 Crypto / secrets: no secrets introduced; runtime hash-lock preserved
- A03 Injection: N/A (admin absolute paths; no new shell interpolation of user input)
- A05 Misconfig: PATH not widened; docker hardening flags intact
- A06 Vulnerable components: lockfile unchanged; CVE audit 须人工核
- A08 Integrity: split download preserves require-hashes for runtime; build tools remain weaker link (P2)
- A10 SSRF: N/A (`--network=none` audit unchanged)

Security Checklist:
- [x] No hardcoded secrets in fix files
- [x] Isolation / PATH not widened
- [x] Runtime hash-lock not bypassed
- [x] Fixture storage_mode does not skip Landlock / does not invent capabilities
- [x] Fail-closed / zero-effect asserts not relaxed
- [ ] Dependencies CVE-audited in this environment (须人工核)
- [x] Local evidence not accepted as Phase 0 formal Go

---

# Critic Reviewer Review Section

Reviewer: Critic-Agent
Time: 2026-08-10 22:05 Asia/Taipei
Verdict: **MERGE local fix (5 files): CONDITIONAL GO / ACCEPT-WITH-RESERVATIONS** · **Phase 0 formal Go: NO-GO / REJECT**
Mode: ADVERSARIAL (process blocker + evidence-labeling risk + CI budget arithmetic; security asserts not weakened)

Pre-commitment vs actual: expected dirty-harness mislabeled as clean/exact-commit, CI timeout hostility, `anchor-reference` coverage hole, digest overclaim, Landlock weakening. Actual: first three confirmed; six-report digests **verified**; silent zero-effect/Landlock weakening **not found** (parent-confirmed). New parent-verified fact: active `git am` session blocks safe commit.

## Contract Consistency

- Acceptance SSOT still requires Layer-3 exact-commit Ubuntu + Layer-4 F01–F04 for formal Go. Local six-report PASS cannot close Phase 0. Wrap-up No-Go on formal Phase 0 is correct.
- CI compare asserts unchanged vs HEAD: `operations == 43`, `unavailable == 11`, `trace.ok`, public `binder == 2`, `landlock_success == 2`, single package/contract digest. Five-file diff does **not** relax these.
- Runtime `PATH=/audit/venv/bin:/usr/bin:/bin` unchanged; `/usr/sbin/{groupadd,useradd}` absolute only — not a PATH widen.
- Fixture `[storage_modes] api = "anchor-reference"` matches created `repositories/api` (no `versions/...`). Enables `_bind_git_metadata`/Landlock instead of pre-binder `RECORD_INVALID`. Default `Line.storage_for` remains `linked-worktree` (`workspace.py`); harness still does not exercise `line_repository_path` — coverage residual, not assertion deletion.
- Handoff §5.4/§5.5 (incomplete wheel/sdist) is stale vs retained six PASS reports; artifacts win.

## Source Evidence Accuracy

Verified `/private/tmp/dyro-bridge-reports.ywZ3zl` (six files): all `passed=true`; 43 ops; 11 unavailable@exit4/ok=false; `trace.ok`; public `binder=2` / `landlock_success=2`; `mutation/network/write_open=0`; package `sha256:baaf9c710d7a32dd332da0987a93a1073e8904e8d7de0d0142d7b118ca25a70e`; contract `sha256:2769249643ca1e03738d0f175c121dd879230ee8740a8fc65f413957c511971e`; `commit=e284c1ce2da731c404ab3866124026a28d03691c`. Digest/parity claims in wrap-up: **accurate**.

Parent-verified labeling fact ( Critic concurs ):

- Reports show `evidence.dirty=clean` (CLI/env asserted via `--dirty clean` / `DYRO_AUDIT_DIRTY=clean`, not measured from git).
- Live `tools/verify_bridge_zero_effects.py` sha256 `7c6057b833efe6813afb904ab9d9de1368b88a658fe9dfa937d996d867bfd2eb` **equals** report `harness.verifier_sha256`.
- `git show HEAD:tools/verify_bridge_zero_effects.py` → `746f86725f5acc98832bc02ac07043121f1dfa8da1cf9aaf30773a24067e8a7d` (**≠** report harness).
- Therefore: valid **本地修复候选证据** only; **invalid** as exact-commit / release evidence. Consumers who trust `dirty=clean` + HEAD SHA without checking harness sha will false-promote Layer-3.

Colima ~8–9m/artifact duration: **须人工核** (report file mtimes are export-time, not audit wall-clock).

## Decision Validity

- Three stated bugs vs fix: `/usr/sbin` absolutes, split `pip download`, fixture `anchor-reference` — all directionally correct; no silent zero-effect/Landlock/fail-closed weakening found.
- Residual (not merge-blockers for security intent): unpinned `setuptools`/`wheel` second download (pre-existing intent); `linked-worktree` path uncovered; regression tests are string/shape guards.
- Phase 0 formal Go remains invalid until committed harness≡package identity, Ubuntu `bridge-zero-effects` artifact, F01–F04, and Final Arbitration ACCEPT.

### Open micro-decisions (Critic)

1. **Timeouts:** Do not raise per-artifact `timeout 8m` or weaken corpus asserts in this fix commit. Job `timeout-minutes: 10` vs three serial Docker builds+runs is arithmetically hostile — treat as follow-up after first Ubuntu wall-clock (agree with Code Contract: not in this commit). Distinguish job-budget hygiene from security-gate relaxation.
2. **Handoff:** Keep out of the five-file fix commit (stale mid-sections). Optional later docs commit under `docs/superpowers/reviews/`.
3. **“本地修复完成”:** Acceptable only as **local repair-candidate verification complete** with mandatory dirty-harness qualifier. Reject bare wording that implies Phase 0 Go or exact-commit CI.

## Plan Executability

**P0 — Stale `git am` session blocks commit path**

- Parent-verified: `git status` reports “You are in the middle of an am session”.
- Any commit / `am --continue` on this worktree risks mixing unrelated patch state with the five-file fix (Code Contract notes rebase-apply patch for already-landed `e7e1225`).
- Fix: `git am --abort` (or equivalent) **before** staging; then stage only the five fix paths. 须人工核 abort does not discard intended WIP outside those paths.
- Until cleared: merge/commit of this fix is **not executable**.

Other executability:

- `ci.yml` push trigger is `main` only; `feat/dev` needs PR for `bridge-zero-effects`.
- Commit/push/PR require separate user authorization.
- Exclude `plans/dyro-agent-bridge-phase-0.md` (user WIP) from the fix commit.

## Scope And Risk

- Five-file diff is tightly scoped (harness/CI/fixture/tests only). No Bridge product runtime modules changed.
- Highest near-term risks: (1) committing during `git am`; (2) promoting dirty-harness reports via `dirty=clean`; (3) CI job timeout on first PR; (4) accidental staging of user WIP plan.
- Security blast radius of the patch itself: low — asserts preserved (aligns with Security-Agent).

## Go/No-Go

| Decision | Verdict | Conditions |
| --- | --- | --- |
| (1) Merge of local fix (5 files) | **CONDITIONAL GO** | Abort `git am` first; stage only five fix files; exclude user WIP plan + handoff from this commit; claims must say candidate/dirty-harness, not exact-commit; no timeout weakening in this commit |
| (2) Phase 0 formal Go | **NO-GO** | Missing exact-commit Ubuntu CI, F01–F04, committed harness≡package, Final Arbitration ACCEPT |

## Required Fixes

1. **[P0/process]** Resolve stale `git am` (`git am --abort` or equivalent) before any commit of these paths.
2. **[P0/claims]** Record completion with harness `verifier_sha256` ≠ HEAD; never cite these six reports as `dirty=clean` exact-commit evidence.
3. **[P1/scope]** Stage only: `ci.yml`, `Dockerfile.audit`, `test_bridge_strace_audit.py`, `test_release_source.py`, `verify_bridge_zero_effects.py`.
4. **[P1/follow-up]** After first Ubuntu CI wall-clock: adjust job/per-run timeouts only with that evidence (open micro-decision #1).
5. **[P1/coverage, not this-commit blocker]** Track `linked-worktree` destination coverage or document why `anchor-reference`-only Landlock proof is accepted for B05.
6. **[P2]** Prefer refreshed handoff / “本地修复候选证据完成；Phase 0 仍为 No-Go” over bare “本地修复完成”.

---

# Final Arbitration

Arbiter: Cursor Root (parent agent)
Time: 2026-08-10 22:10 Asia/Taipei
Final verdict: **Conditional Go for merging the 5-file local fix** · **No-Go for Phase 0 formal release**

## 1. Final Verdict

- May the local-fix commit proceed: **Conditional Go** (process preconditions below)
- May Phase 0 be declared formal Go / publishable: **No-Go**
- Required preconditions before commit:
  1. Clear stale `git am` (`git am --abort` or equivalent) — **须人工核** abort does not discard intended WIP
  2. Stage only the five fix files; exclude `plans/dyro-agent-bridge-phase-0.md`
  3. Keep claim language as **本地修复候选证据**; do not cite six reports as exact-commit / release evidence
  4. Do **not** widen CI timeouts in this commit
- Blocking reasons for Phase 0 formal Go: F01–F04 host evidence missing; exact-commit Ubuntu CI missing; harness sha ≠ HEAD while reports assert `dirty=clean`; independent review gate previously open (this board closes the *review* gate for the local-fix scope only)

## 2. Repo / Module Go-No-Go

| Repo/Module | Spec | Plan | Verdict | Reason |
| --- | --- | --- | --- | --- |
| 5-file local fix (CI / Dockerfile / fixture / tests) | N/A (bugfix) | Handoff Steps 0–6 | **Conditional Go** | Fixes match diagnosed bugs; security gates preserved; process blockers remain |
| Local Docker six-report evidence | Acceptance Layer-2/local | Handoff §5 | **Accept as candidate only** | PASS+parity proven; dirty harness ≠ exact-commit |
| Phase 0 formal release | Acceptance Layer-3/4 | F01–F04 + CI | **No-Go** | Host + Ubuntu exact-SHA gates open |
| Timeout change in this commit | CI budget | Micro-decision #1 | **No-Go (do not change now)** | Need Ubuntu runner wall-clock first |

## 3. P0 Required Fixes

### P0-F1: Clear stale `git am` before any commit

Evidence:
- `git status`: “You are in the middle of an am session.”
- Worktree gitdir `rebase-apply/0001` is the already-landed `e7e1225` patch (dated 2026-08-07); `next=1` / `last=1`.

Decision:
- Abort the stale am session before staging/committing the five-file fix.
- Do not `am --continue` that patch.

Acceptance:
- `git status` no longer reports an am session; five fix files remain as intended WIP; user plan WIP still present if desired.

### P0-F2: Do not promote local reports to exact-commit / release evidence

Evidence:
- All six `/private/tmp/dyro-bridge-reports.ywZ3zl/*-report.json`: `passed=true`, digests match wrap-up claims, `evidence.dirty=clean`, `evidence.commit=e284c1c…`.
- `evidence.harness.verifier_sha256=7c6057b8…` equals live dirty `tools/verify_bridge_zero_effects.py`.
- `git show HEAD:tools/verify_bridge_zero_effects.py` → `746f8672…` (different).
- `DYRO_AUDIT_DIRTY=clean` is env-asserted, not measured from git.

Decision:
- Severity split (arbiter): **P0 against formal Go / release marketing**; **not a code defect in the five-file fix**.
- Keep handoff wording: 本地修复候选证据 only.
- After commit, regenerate audits from clean checkout of that SHA for Layer-3.

Acceptance:
- Any completion report / commit message / PR body that cites these six reports must include dirty-harness qualifier and deny exact-commit status.

## 4. P1 / P2

### P1 (must handle in commit hygiene or immediate follow-up)

1. **Stage scope:** only `.github/workflows/ci.yml`, `tests/fixtures/bridge/Dockerfile.audit`, `tests/test_bridge_strace_audit.py`, `tests/test_release_source.py`, `tools/verify_bridge_zero_effects.py`.
2. **CI wall-clock risk:** job `timeout-minutes: 10` vs three serial `timeout 8m` docker runs (+ builds) is arithmetically hostile. Record as known risk; adjust only after first Ubuntu failure/success evidence. (Downgraded from Critic “P0 formal” framing for *this fix merge* — it does not make the patch incorrect.)
3. **Claim language:** prefer “本地修复候选证据完成；Phase 0 仍为 No-Go” over bare “本地修复完成”.
4. **Coverage residual:** fixture now only exercises `anchor-reference` Landlock path; track `linked-worktree` coverage as follow-up (not a silent gate weaken).

### P2 (harden soon; not merge-blockers)

1. Assert runtime PATH remains narrow and does not gain `/usr/sbin` (Security P2-1).
2. Strengthen hash-lock string tests / later pin+hash or isolate build-tool wheelhouse (Security P2-2/3).
3. Refresh or relocate handoff docs; optional separate docs commit for this board file.
4. Regression tests remain string/shape guards; Docker black-box stays Ubuntu CI’s job.

## 5. Open Micro-Decisions (resolved)

1. **CI timeouts:** **Only after** real `ubuntu-24.04` wall-clock evidence (failure or proven margin). Do not change in the fix commit.
2. **Handoff markdown:** **Keep out** of the five-file fix commit. This board file may be a later docs commit; handoff may stay untracked or move under `docs/superpowers/reviews/`.
3. **“本地修复完成” terminology:** **Acceptable with qualifier** = local repair-candidate verification complete. **Unacceptable** as Phase 0 formal Go.

## 6. Instructions For The Execution Agent

When user authorizes commit (separately):

1. Ask/confirm `git am --abort` (do not abort without authorization if user has other intent).
2. Re-check `git status` clean of am session.
3. Stage only the five fix files.
4. Commit with Chinese Conventional Commit subject, e.g. `fix: 修复 Agent Bridge 零副作用审计运行时路径与 fixture 契约`.
5. Stop; ask separately for push; then separately for PR.
6. Do not delete Docker images, evidence volumes, or `/private/tmp` audit contexts.
7. Do not call dispatch / objective apply / merge / release / publish.

## 7. Conditions To Start Implementation

N/A for new feature work. For **landing this fix**:

- P0-F1 cleared
- Stage scope correct
- Claim language correct
- No timeout weakening in the same commit

## 8. Requires Human Verification

- Aborting `git am` does not discard intended non-fix WIP (**须人工核**)
- F01–F04 real Codex host journeys (**须人工核** / host-only)
- Exact-commit Ubuntu `bridge-zero-effects` artifact after commit+PR (**须人工核**)
- Optional: `pip-audit` on locked export on healthy runner (**须人工核**)
- Colima vs GHA wall-clock margin (**须人工核** on first PR)

## 9. Reviewer Conflict Resolution

| Topic | Code-Contract | Security | Critic | Arbiter |
| --- | --- | --- | --- | --- |
| Merge this fix | Conditional Go | Go | Conditional Go | **Conditional Go** |
| Phase 0 formal Go | No-Go | No-Go | No-Go | **No-Go** |
| Security gate weakening | Not found | Not found | Not found | **Not found** |
| `git am` severity | P1 process | (not primary) | P0 process | **P0 process (commit blocker)** |
| dirty-harness / `dirty=clean` | P1 claims | claim inflation HIGH if misused | P0 claims | **P0 vs formal Go; P1 for labeled candidate merge** |
| CI timeout | P1 follow-up | operational | Critical/Major framing | **P1 follow-up; no change now** |
| Six-report PASS/digests | Proven | Proven | Proven | **Proven as candidate evidence** |

## 10. Source-Verified Facts (arbiter re-check)

- HEAD / upstream: `e284c1ce2da731c404ab3866124026a28d03691c`
- Six reports PASS with stated package/contract digests
- public binder=2, landlock_success=2, mutation=network=write_open=0
- Harness verifier sha matches dirty WIP, not HEAD
- Active `git am` confirmed via status + `rebase-apply` contents for landed `e7e1225`
- No Bridge product runtime modules in the five-file diff

Final signature: Cursor Root · 2026-08-10
