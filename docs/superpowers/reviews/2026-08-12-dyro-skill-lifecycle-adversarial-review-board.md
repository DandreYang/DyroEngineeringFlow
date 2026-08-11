# Dyro Skill Lifecycle Adversarial Review Board

Date: 2026-08-12

Scope:
- Repository: `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev/dyroengineeringflow`
- Branch: `feat/dev` (uncommitted working tree on top of `7bb8c64`)
- Question: Are the Skill lifecycle hooks (setup guide + post-update sync + startup repair) safe and ready to land?

Reviewed Materials:
- Working tree diff (exclude user WIP plans): `/tmp/dyro-skill-lifecycle.diff`
- `src/dyro/cli.py` (`_setup_skill_preference`, `_apply_setup_personal_preferences`, `cmd_integration_sync`, `_refresh_skill_via_new_cli`, `_maybe_sync_managed_skill`, daily-update wiring)
- `src/dyro/integrations/manager.py` (`sync_managed_skill`)
- `src/dyro/integrations/__init__.py`
- Tests: `tests/test_cli.py`, `tests/test_integrations.py`, `tests/test_updates.py`
- Docs: `CHANGELOG.md` Unreleased, `docs/updates.md`, localized READMEs

SSOT:
- Product: setup guided first install; package update syncs **managed** Skill only; interactive startup repairs **OUTDATED** managed Skill; never first-install on upgrade/startup
- Ownership/fail-closed Skill install from 0.6.3 (legacy target allowlist, packaged-asset check) remains binding
- Preview-first / `--yes` for explicit install; automatic paths may use `--yes` only for managed upgrade/repair

## Rules

1. Each reviewer writes only in their own signed section.
2. Conflicts are resolved by source code or live contract.
3. Unprovable claims are marked `须人工核`.
4. Findings use P0/P1/P2 severity.
5. Code-review mode: bugs, security, regressions, missing tests, misleading UX first.
6. Do not reopen “silent first-install for everyone” unless source proves current opt-in is unsafe.

## Fixed Decisions

- First-time Skill install remains opt-in (setup or explicit CLI).
- Post-update / startup paths must not first-install absent Skills.
- Post-update sync should use fresh `dyro` entry point when possible (new package assets).
- Conflict / recovery / unowned states remain fail-closed (soft warn, no mutate).

## Open Micro-Decisions

1. Should startup Skill sync share the daily-update interactive gate only, or also run for other interactive commands?
2. When post-update CLI sync fails, is “retry next launch” enough, or must update exit non-zero?
3. Should non-interactive `dyro setup --non-interactive` gain an explicit `--install-skill` flag in this change?

---

# Claude Review Section

Reviewer: Claude (Composer)
Time: 2026-08-12
Verdict: Conditional Go

## Summary

End-to-end contract is **correct in source**: `sync_managed_skill` gates `ABSENT` on `allow_first_install`; setup uses preview (`dry_run=True`) then apply (`yes=True`) only after plan confirmation; post-update and startup paths pass `allow_first_install=False`; startup repair triggers **only** on `IntegrationState.OUTDATED`. No path silently first-installs an `ABSENT` Skill on upgrade or launch. Remaining issues are UX accuracy and test gaps, not lifecycle safety.

## Evidence

### P0 — None

No blocker found. `ABSENT` + `allow_first_install=False` returns `None` before `install_integration` (`manager.py:1035-1036`). Startup gate is `status.state is not IntegrationState.OUTDATED` (`cli.py:3855-3856`). Post-update subprocess invokes `integration sync skill --yes` (`cli.py:3827-3828`), which sets `allow_first_install=False` (`cli.py:1560-1563`).

### P1

| ID | Finding | Evidence |
|----|---------|----------|
| P1-1 | **Setup completion misreports Skill outcome on soft-fail.** `_apply_setup_personal_preferences` prints a warning and returns early on `DyroError`, but callers always invoke `_print_setup_completion`, which prints `Skill：已请求安装 / 同步` whenever `preferences.install_skill` is true—never whether apply succeeded. | `cli.py:659-669`, `cli.py:782-783`, `cli.py:867-868`, `cli.py:912-913` |
| P1-2 | **Post-update Skill sync is untested at the subprocess boundary.** `_refresh_skill_via_new_cli` is wired from `cmd_update_now` and `_maybe_run_daily_update` (`cli.py:1618-1619`, `3810`) but has no direct test (only mock-assert in auto-patch test). Subprocess failure modes (non-zero exit, timeout, missing `dyro` on PATH) are unverified. | `cli.py:3815-3846`, `tests/test_updates.py:520-521` (mock only) |

### P2

| ID | Finding | Evidence |
|----|---------|----------|
| P2-1 | **Startup OUTDATED repair includes legacy Codex migration without session opt-in.** Pre-existing state machine marks legacy installs `OUTDATED` when manifest is absent (`manager.py:566-574`); new startup hook auto-mutates with `--yes`. Not `ABSENT` first-install, but silent migration—align docs if intentional. | `manager.py:566-574`, `cli.py:3849-3872` |
| P2-2 | **Same-session post auto-patch drift if subprocess sync fails.** After in-process auto-patch, running interpreter still holds old `ASSET_VERSION`; `_maybe_sync_managed_skill` may see `CURRENT` and skip. Recovery depends on subprocess refresh or next launch. Acceptable per "retry next launch" but worth documenting. | `cli.py:3796-3810`, `3855-3856`, `manager.py:699-730` |
| P2-3 | **Test gaps:** no test that conflict states (`DRIFTED`, `UNOWNED_CONFLICT`, etc.) force `_setup_skill_preference()` → `False`; no end-to-end interactive setup Skill preview→apply test; no CLI test for `cmd_integration_sync`. | `cli.py:567-580`, `tests/test_cli.py:573-612` |
| P2-4 | **`cmd_integration_sync` preview path:** when `plan is None`, message conflates ABSENT and CURRENT ("未安装或已是当前版本"). Accurate but coarse for operator debugging. | `cli.py:1565-1566` |

## Verified Contracts (pass)

| Path | `allow_first_install` | First-install possible? |
|------|----------------------|-------------------------|
| Setup preview | `True`, `dry_run=True` | Preview only, no writes (`cli.py:640`) |
| Setup apply | `True`, `yes=True` | Yes, after user confirms plan (`cli.py:661`, `904-912`) |
| `integration sync` CLI | `False` | No (`cli.py:1560-1563`) |
| Post-update subprocess | via sync CLI → `False` | No (`cli.py:3827-3828`) |
| Startup `_maybe_sync_managed_skill` | `False`, OUTDATED only | No (`cli.py:3855-3859`) |
| Setup dry-run | apply skipped | No (`cli.py:901-903`, `824-826`) |
| Non-interactive setup | skill preference not invoked | No (`cli.py:1016+`) |

Setup conflict/recovery states are fail-closed in the preference step (`cli.py:567-580`), consistent with SSOT.

## Required Fixes (for unconditional Go)

1. **P1-1:** Track Skill apply outcome (success / soft-fail / skipped) and reflect it in `_print_setup_completion` instead of echoing `preferences.install_skill` alone.
2. **P1-2:** Add tests for `_refresh_skill_via_new_cli`: success stdout passthrough, non-zero exit warning, timeout/OSError, missing `dyro` on PATH; assert `cmd_update_now` calls it only when `perform_update` returns `True`.

## Open Micro-Decisions — Vote

| # | Question | Vote | Rationale |
|---|----------|------|-----------|
| 1 | Startup sync: share daily-update gate only? | **Yes — keep current gate** | Matches docs (`dyro` / `home` / `start`, interactive, not dry-run). `_should_run_daily_update` at `cli.py:3780-3784`; hook at `3888-3890`. Broader scope adds surprise mutations on administrative commands. |
| 2 | Post-update sync failure: "retry next launch" enough? | **Yes** | Package update already succeeded; non-zero exit would punish a partial best-effort. Subprocess + startup OUTDATED repair provide two retries (`cli.py:3837-3842`, `3857-3865`). |
| 3 | `--install-skill` for non-interactive setup in this change? | **Defer** | Non-interactive path correctly skips Skill today (`1016+`). Flag is useful but orthogonal; ship lifecycle hooks first. |

---

# OpenCode Review Section

Reviewer: OpenCode (Composer)
Time: 2026-08-12
Verdict: **Request changes — do not land until P0 is fixed**

## P0

**1. Same-session stale overwrite after successful auto-update refresh**

**Evidence chain:**

- `src/dyro/cli.py` `main()` (L3888–3890): on interactive `dyro` / `home` / `start`, unconditionally runs `_maybe_run_daily_update()` then `_maybe_sync_managed_skill()` in the same process turn.
- `src/dyro/cli.py` `_maybe_run_daily_update()` (L3796–3810): when `auto_patch` and patch available, calls `perform_update(..., yes=True)`; on `updated=True`, calls `_refresh_skill_via_new_cli()` then returns — but does **not** skip the subsequent `_maybe_sync_managed_skill()` in `main()`.
- `src/dyro/cli.py` `_refresh_skill_via_new_cli()` (L3815–3846): spawns subprocess `[dyro_bin, "integration", "sync", "skill", "--yes"]` using a **new** `dyro` entry point (fresh wheel assets on disk).
- `src/dyro/cli.py` `_maybe_sync_managed_skill()` (L3849–3872): if `integration_status("skill").state is IntegrationState.OUTDATED`, calls `sync_managed_skill(yes=True, allow_first_install=False)` **in-process** via `install_integration()`.
- `src/dyro/integrations/manager.py` `integration_status()` (L699–730): freshness compares manifest `asset_digest` against `_asset_digest(_asset_inventory())` loaded from the **currently running** Python package (`ASSET_VERSION`, packaged skill files).

**Failure mode:**

1. Old in-process Dyro (pre-patch) runs `perform_update()`; disk now has new package.
2. Subprocess refresh writes Skill mirror/manifest with **new** package `asset_digest`.
3. Old process still loads `_asset_inventory()` from **old** wheel; manifest digest ≠ old desired digest → status `OUTDATED` (L724–730).
4. `_maybe_sync_managed_skill()` reinstalls Skill content from **stale** in-process assets, overwriting the subprocess sync.

This violates board SSOT (“post-update sync should use fresh `dyro` entry point when possible”) and can silently regress Skill assets on the launch that auto-patched.

**Required fix:** Skip `_maybe_sync_managed_skill()` when auto-update or post-update refresh already ran in the same `main()` turn; or re-exec into the new entry point before any in-process Skill mutation; or have `_maybe_run_daily_update()` return a flag consumed by `main()`.

## P1

**2. `_refresh_skill_via_new_cli()` PATH binding not tied to upgrade target** (`src/dyro/cli.py` L3817–3828)

Uses `shutil.which("dyro")` with inherited `PATH`. After `pip --user`, `pipx`, or `uv tool` upgrade, the first `dyro` on `PATH` may not be the installation just updated (multiple installs, shadowed `~/.local/bin`, dev venv vs global wrapper). Subprocess may invoke wrong CLI/assets while parent treats refresh as best-effort complete.

**Required fix:** Derive entry point from active install context (`build_update_plan` / `sys.prefix` / pipx-venv bin); pass explicit `env`; do not rely on bare `which("dyro")`.

**3. Recursion correctly avoided; fallback still uses stale assets**

Subprocess `integration sync` does not re-enter daily update (`_should_run_daily_update()` L3780: `command` must be `{None, "home", "start"}`). When subprocess refresh **fails**, `_maybe_sync_managed_skill()` still runs in-process with old assets — acceptable until restart, but P0 “success then overwrite” is strictly worse.

**4. Test coverage gaps on automatic paths**

Present: `tests/test_updates.py` mocks `_refresh_skill_via_new_cli` on daily auto-patch success/failure; `test_startup_syncs_outdated_managed_skill`; `tests/test_integrations.py` `test_sync_managed_skill_*`.

Missing (required before ship):

| Gap | Risk |
|-----|------|
| No test that `_refresh_skill_via_new_cli` invokes expected argv / handles non-zero exit / timeout | Subprocess regressions undetected |
| No test that `cmd_update_now` (L1618–1619) calls refresh when `perform_update` returns `True` | Wiring drift vs daily path |
| **No regression test for P0** (auto-update + successful refresh → `_maybe_sync_managed_skill` must not run in-process) | P0 can reappear |
| No CLI tests for `cmd_integration_sync` (L1557–1570): preview gate, `--yes`, `--dry-run`, ABSENT no-op | DRY-RUN/`--yes` contract untested |
| No test that refresh is not called on dry-run / failed update | False-positive side effects |

## P2

**5. DRY-RUN / `--yes` semantics: install vs sync**

Shared preview gate in `cmd_integration_install` (L1549–1554) and `cmd_integration_sync` (L1557–1570):

```python
preview = args.dry_run or not args.yes
```

- `install`: always produces a plan via `install_integration()` including `ABSENT`; preview prints `DRY RUN:` — good.
- `sync`: `sync_managed_skill(..., allow_first_install=False)` (`manager.py` L1033–1036) returns `None` for `ABSENT`/`CURRENT`; CLI prints `无需同步；Skill 未安装或已是当前版本。` — correct upgrade-only semantics, conflates two states, not CLI-tested.
- `--yes` + `--dry-run` → preview-only, no writes — matches install/uninstall — good.
- Automatic paths (`_refresh_skill_via_new_cli`, `_maybe_sync_managed_skill`, setup apply via `_apply_setup_personal_preferences` L659–673) bypass preview/`--yes` for managed repair only — aligned with board SSOT.

**6. Minor UX / docs**

- Setup preview uses `sync_managed_skill(yes=False, dry_run=True, allow_first_install=True)` (`cli.py` L640); apply uses `yes=True` (L659); dry-run setup returns before apply (L901–903) — verified.
- Localized READMEs and `docs/updates.md` document `integration sync skill --yes`; no CLI test locks upgrade-only contract.

## Required Fixes (summary)

1. **P0:** Prevent `_maybe_sync_managed_skill()` in `main()` (L3890) after successful auto-update / post-update refresh in the same invocation.
2. **P1:** Resolve post-update `dyro` binary from upgrade target, not global `PATH`/`which`.
3. **P1:** Add tests for `_refresh_skill_via_new_cli`, `cmd_update_now` → refresh, P0 regression, and `integration sync` CLI preview/`--yes`/`--dry-run`.
4. **P2:** CLI test or clearer messaging distinguishing `ABSENT` vs `CURRENT` on `integration sync`.

---

# Hermes Review Section

Reviewer: Hermes (Security)
Time: 2026-08-12
Verdict: **Conditional Go**

## Hunt Results (evidence-backed)

### P0 — None

No remotely exploitable path found that first-installs on startup/update, bypasses legacy allowlist / packaged-asset checks, or overwrites foreign skills via the new auto-`yes` paths. `sync_managed_skill()` delegates to `install_integration()`; conflict/recovery states still hit `_require_mutable_state()` fail-closed (`manager.py:898-914`, `1037-1044`). Foreign avatar protection remains intact (`manager.py:969-991`; tests `test_unowned_conflict_is_never_overwritten_or_removed`, `test_forged_legacy_over_foreign_avatar_is_refused` in `tests/test_integrations.py`).

### P1 — Post-update subprocess trusts `PATH` for `dyro` binary

**Category:** A08 Integrity / local execution hijack
**Location:** `cli.py:3815-3833` (`_refresh_skill_via_new_cli`)
**Exploitability:** Local; attacker who can prepend to `PATH` (or win a race right after package update) before auto-sync runs
**Blast radius:** Arbitrary code execution as the user, invoked with fixed args `[dyro, integration, sync, skill, --yes]` — can mutate managed Skill mirror/avatars under that identity

**Evidence:** After `perform_update()` / auto-patch (`cli.py:1618-1619`, `3808-3810`), code resolves the binary via `shutil.which("dyro")` and executes it. Args are list-form (no shell injection — good), but the **binary choice is unconstrained**. A trojan `dyro` on `PATH` is fully trusted.

**Remediation:**
```python
# BAD — trusts PATH
dyro_bin = shutil.which("dyro")
subprocess.run([dyro_bin, "integration", "sync", "skill", "--yes"], ...)

# GOOD — pin to the interpreter/entry point that just updated
subprocess.run(
    [sys.executable, "-m", "dyro.cli", "integration", "sync", "skill", "--yes"],
    env={**os.environ, "PATH": sanitized_minimal_path},
    ...
)
```

**Condition to ship:** Pin post-update re-exec to the freshly installed entry point (or pass an explicit resolved path from the updater), not bare `which("dyro")`.

### P1 — Startup auto-sync mutates agent homes with `yes=True` and no per-run preview

**Category:** A04 Insecure Design / consent boundary (managed-only)
**Location:** `cli.py:3849-3872` (`_maybe_sync_managed_skill`), wired from `main()` at `3888-3890`
**Exploitability:** Local interactive user; runs on every interactive `dyro` / `dyro home` / `dyro start` when state is `OUTDATED`
**Blast radius:** Atomic mirror upgrade + avatar repair across detected agent homes for **already-managed** installs only (`allow_first_install=False`)

**Evidence:** Gate is correct for scope — `_should_run_daily_update()` limits to `{None, home, start}` + TTY (`3772-3784`); absent Skills are skipped (`manager.py:1035-1036`). Foreign paths still fail-closed or are skipped (`manager.py:987-991`).

**Residual risk:** By product intent, but still privileged mutation without a dry-run/confirm step on each launch. Acceptable only if changelog/docs clearly state “interactive startup auto-repairs managed Skill.”

**Condition to ship:** Document the behavior prominently (partially done in `CHANGELOG.md` / `docs/updates.md`); consider logging a one-line audit trail of changed paths.

### P2 — Auto-patch bundles silent Skill `--yes` sync

**Category:** A04 / consent chaining
**Location:** `cli.py:3796-3810` → `_refresh_skill_via_new_cli()`
**Evidence:** User enabling `auto_patch` consents to `perform_update(..., yes=True)`; on success, Skill sync also runs with `--yes` without a separate prompt. Mitigated because subprocess sync path sets `allow_first_install=False` (`cmd_integration_sync`, `cli.py:1557-1564`).
**Remediation:** Mention in setup copy that auto-patch includes managed-Skill sync; optional env opt-out (e.g. `DYRO_NO_SKILL_SYNC`).

### P2 — Setup skill question nudges install on Enter

**Category:** A04 / consent UX
**Location:** `cli.py:589-597` (`default="1"`)
**Evidence:** Empty input selects install. **Mitigated** by later plan preview (`640-647`) and final apply gate `_ask_yes_no(..., default=False)` (`994-996`, `904-906`). Not a bypass, but increases mis-click/Enter-through risk before the hard stop.
**Remediation:** Default skill choice to `"2"` (defer) or require non-empty confirmation for option 1.

### P2 — Missing adversarial tests for sync fail-closed on conflict states

**Category:** A05 Security Misconfiguration / test gap
**Location:** `tests/test_integrations.py` (only `ABSENT` skip + `OUTDATED` upgrade covered at `595-611`)
**Evidence:** `install_integration` refusal for `UNOWNED_CONFLICT` / `DRIFTED` is tested (`161-183`), but **`sync_managed_skill()` is not explicitly tested** to propagate those failures on auto/CLI sync paths.
**Remediation:** Add tests: `sync_managed_skill(yes=True, allow_first_install=False)` raises/soft-fails on `DRIFTED`, `UNOWNED_CONFLICT`, `RECOVERY_REQUIRED`.

### P2 — Cleared hunts

- **Subprocess injection:** argv lists, no `shell=True`, no user-controlled args (`cli.py:3827-3828`, `manager.py:272-273`).
- **Startup scope / non-interactive gating:** sync only on interactive `{None, home, start}`; not on `setup`, `integration`, `update`, etc. (`3772-3790`, `3888-3890`).
- **Legacy allowlist / packaged-asset bypass:** `sync_managed_skill()` → same `_legacy_owned_copy(..., require_current_assets=True)` and avatar allowlist as manual install (`manager.py:453-481`, `954-978`, `1011-1044`).
- **First install without consent on startup/update:** all automatic paths use `allow_first_install=False`; first install only via setup + final confirm or `integration install --yes`.

## Security Checklist

- [x] No hardcoded secrets in diff
- [x] Automatic `yes=True` paths cannot first-install absent Skills
- [x] Foreign-skill overwrite protections preserved in `manager.py`
- [x] Legacy allowlist / packaged-asset checks not bypassed by sync
- [x] Subprocess args not injectable (list argv, no shell)
- [ ] Post-update re-exec pinned to trusted entry point (**P1 open**)
- [ ] Adversarial tests for sync on conflict/recovery states (**P2 open**)
- [ ] Dependency audit not run in review environment (须人工核)

**Ship recommendation:** **Conditional Go** — land after PATH-pinned post-update re-exec (P1). P2 items are hardening/docs/tests, not blockers if P1 is fixed or explicitly accepted with a tracked follow-up.

---

# Agy Review Section

Reviewer: Agy
Time: 2026-08-12
Verdict: Conditional Go

## Product / UX Findings

### P1 — No-host setup still recommends a known-fail path
Evidence: `src/dyro/cli.py` `_setup_skill_preference` — when `status.avatars` is empty, option 1 is `尝试安装（无宿主时会失败并提示，可稍后重试）（推荐）` with `default="1"`. Apply then hits manager fail-closed (`没有可挂接的宿主分身；拒绝只安装孤立镜像`) and soft-fails.
Issue: Copy discloses failure, but “推荐” + Enter-default still steers users into a guaranteed soft-fail on hostless machines. That is not honest recommendation semantics.
Required fix: If no hosts, remove `（推荐）` from option 1, default to option 2 (“稍后手动安装”), and/or rephrase option 1 as non-recommended “仍要尝试（预期失败）”.

### P1 — Setup completion overclaims after soft-fail
Evidence: `_apply_setup_personal_preferences` warns and returns on `DyroError`; `_print_setup_completion` still prints `Skill：已请求安装 / 同步` whenever `preferences.install_skill` is true.
Issue: End-of-setup summary reads as “we took your install request seriously / it is in flight,” not “request failed; Skill absent.”
Required fix: Completion must reflect outcome: success / skipped-current / failed-soft (with the same remediation tip). Prefer tracking apply result, not the preference bit alone.

### P1 — Plan preview can look actionable when apply cannot succeed
Evidence: `_render_setup_personal_preferences` summarizes `安装 / 同步控制面 Skill（镜像 + 宿主分身）` while `plan_integration` for ABSENT+no hosts lists `未检测到宿主目录…` plus `（预览）将创建镜像` / manifest; apply refuses orphan mirror.
Issue: Summary line overpromises “宿主分身”; preview bullets mix blocker with optimistic “将创建” language. Confirming the plan feels like approving a real install.
Required fix: When no hosts, plan summary must lead with blocker (e.g. “无法安装：未检测到宿主”) and avoid “将创建镜像/分身” wording that implies a successful write path.

### P2 — Soft-fail messaging quality (mostly good; small gaps)
Good: setup apply soft-fail + `install --dry-run` tip; post-update `_refresh_skill_via_new_cli` best-effort warnings; startup repair soft-fail + `sync --dry-run`.
Gaps:
- Post-update “下次启动将重试” is only true for interactive `dyro` / `home` / `start` (same `_should_run_daily_update` gate), and is skipped when `DYRO_NO_UPDATE_CHECK` is set—undocumented coupling.
- `cmd_integration_sync` prints `无需同步；Skill 未安装或已是当前版本` — collapses two meanings; hurts sync vs install discoverability.

Required fix (P2): Split sync no-op copy (`未安装，请用 install` vs `已是当前版本`); document that `DYRO_NO_UPDATE_CHECK` also skips startup Skill repair, or decouple the gates (see micro-decision 1).

### P2 — Docs vs behavior / daily-update story
Accurate: `docs/updates.md` Control-plane Skill section matches SSOT (setup opt-in, post-update managed sync via fresh entrypoint, startup OUTDATED repair, no first-install on upgrade/startup). CHANGELOG Unreleased matches. Daily PyPI check story (once/local day, non-blocking, confirm-by-default, patch auto opt-in) remains accurate; Skill hooks are additive.
Gaps:
- README locales describe auto sync/repair but mostly omit `dyro integration sync skill` (discoverability of sync vs install weaker than `docs/updates.md`).
- README “Daily check” narrative unchanged; Skill repair sharing that interactive gate / env opt-out is easy to miss.
- `README.pt-BR.md`: `repararam` → should be `reparam` (grammar).

Required fix (P2): One README sentence naming `install` (first-time) vs `sync` (managed upgrade-only); note env/gate coupling if kept; fix pt-BR typo.

### P2 — Sync vs install discoverability
CLI `integration sync` help (`仅升级已托管的 Skill`) is clear; docs/updates comment helps. Setup conflict path only points at `install`, which is correct for first-time/conflict. Main gap is README + sync no-op copy (above).

## Micro-decision votes

1. **Startup Skill sync gate**
   Vote: Keep command scope to interactive `dyro` / `home` / `start` only (do not expand to arbitrary interactive commands). Prefer decoupling Skill repair from `DYRO_NO_UPDATE_CHECK` (update opt-out should not silently disable Skill repair), or document the coupling explicitly in `docs/updates.md`.

2. **Post-update sync failure vs exit code**
   Vote: “Retry next launch” is enough. Do not make package update exit non-zero when companion Skill sync fails (best-effort companion; update success remains the primary outcome). Keep visible warning.

3. **`dyro setup --non-interactive --install-skill`**
   Vote: Out of scope for this change. Keep non-interactive free of Skill side effects; first install stays interactive setup or explicit `dyro integration install skill --yes`.

## Go / No-Go

- Go / No-Go: Conditional Go
- Blocking for honest UX: P1 recommendation default + completion honesty + no-host plan wording.
- Not blocking: P2 docs/discoverability polish, sync no-op copy, pt-BR typo.

## Required Fixes (executable)

1. No hosts → do not mark install as recommended; default to defer.
2. Setup completion reflects install outcome, not preference alone.
3. No-host plan summary/blocker-first; drop optimistic “将创建…” success framing.
4. (P2) Clarify sync no-op; README install vs sync; document or decouple `DYRO_NO_UPDATE_CHECK` vs Skill repair; fix pt-BR.

---

# Grok Review Section

Reviewer: Grok
Time: 2026-08-12
Verdict: Go

## Scorecard

| # | Claim | Result |
|---|--------|--------|
| 1 | `sync_managed_skill(allow_first_install=False)` never installs ABSENT | **PASS** |
| 2 | `_maybe_sync_managed_skill` only acts on OUTDATED | **PASS** |
| 3 | Setup can first-install only after plan confirm | **PASS** |
| 4 | Post-update uses subprocess sync not in-process old assets | **PASS** |
| 5 | Daily update check still gated to interactive home/start/default | **PASS** |

## Findings (source-verified)

1. **PASS** — `manager.py` `sync_managed_skill`: ABSENT + `not allow_first_install` → `return None` before `install_integration`. Covered by `test_sync_managed_skill_skips_absent_without_first_install`.

2. **PASS** — `cli.py` `_maybe_sync_managed_skill`: early return unless `status.state is IntegrationState.OUTDATED`; sync call uses `allow_first_install=False` (defense in depth).

3. **PASS** — Interactive setup: preference → plan render (dry-run preview with `allow_first_install=True`) → `_ask_yes_no` / `--yes` → `_apply_setup_personal_preferences` → mutating sync. Non-interactive setup never calls skill install. Only mutating `allow_first_install=True` site is post-confirm apply.

4. **PASS** — `_refresh_skill_via_new_cli` uses `subprocess.run([dyro_bin, "integration", "sync", "skill", "--yes"])`; `cmd_integration_sync` sets `allow_first_install=False`. Wired from `cmd_update_now` and patch auto-update success. No in-process `sync_managed_skill` on those paths.

5. **PASS** — `_should_run_daily_update` requires `command in {None, "home", "start"}`, TTY interactive, not dry-run, not `DYRO_NO_UPDATE_CHECK`. Sole call site in `main()`.

## P2 (non-blocking)

- **P2:** `_refresh_skill_via_new_cli` resolves via `shutil.which("dyro")`, not Scripts next to the updated `sys.executable`; wrong PATH shadow could sync via a different binary (still subprocess; soft-fail if missing).
- **P2:** No direct unit test that `_maybe_sync_managed_skill` no-ops on ABSENT (logic is clear; only OUTDATED path tested).

---

# Final Arbitration

Arbiter: Cursor Root (parent agent)
Time: 2026-08-12 Asia/Taipei
Final verdict: **No-Go for land** until P0 closed · **Conditional Go** after P0 + listed P1s

## 1. Final Verdict

- May this Skill lifecycle WIP land as-is: **No-Go**
- After P0 + required P1s: **Conditional Go**
- First-install / fail-closed ownership contracts: **hold** (Grok scorecard 1–3/5 + Hermes clear; not reopened)
- Blocking reason: OpenCode P0 same-session stale overwrite is **source-verified**

## 2. Seat Summary

| Seat | Verdict | Arbiter note |
| --- | --- | --- |
| Claude | Conditional Go | Correct on consent gates; missed P0 overwrite chain |
| OpenCode | Request changes (P0) | **Upheld** — P0 is decisive |
| Hermes | Conditional Go | No security P0; PATH pin upheld as P1 integrity |
| Agy | Conditional Go | UX honesty P1s upheld (not merge-blockers once P0 fixed, but must-fix before “honest setup” claim) |
| Grok | Go | Scorecard PASS upheld for allow_first_install; claim #4 incomplete — did not examine post-refresh in-process follow-up |

## 3. P0 Required Fixes

### P0-F1: Same-session stale Skill overwrite after auto-patch refresh

Evidence:
- `cli.py` `main()` always runs `_maybe_sync_managed_skill()` after `_maybe_run_daily_update()`
- Successful auto-patch → `_refresh_skill_via_new_cli()` writes **new** digests via subprocess
- Still-running old interpreter then evaluates `integration_status` against **old** `_asset_inventory()` → `OUTDATED` → in-process `install_integration` overwrites with stale assets (`manager.py:699-730`)

Decision:
- After a successful package update + Skill refresh in the same process turn, **must not** run in-process `_maybe_sync_managed_skill()`
- Minimal fix: `_maybe_run_daily_update()` returns whether refresh already ran (or update succeeded); `main()` skips startup sync when true
- Acceptable alternate: re-exec into new entry point before any in-process Skill mutation

Acceptance:
- Unit/integration test: auto-patch success + mocked successful `_refresh_skill_via_new_cli` ⇒ `_maybe_sync_managed_skill` / in-process `sync_managed_skill` **not** invoked
- Manual mental model: “fresh subprocess sync is last writer in that turn”

## 4. P1 Required Before Unconditional Land

### P1-F1: Setup completion honesty (Claude P1-1 + Agy)
- Track Skill apply outcome; `_print_setup_completion` must not claim `已请求安装 / 同步` after soft-fail

### P1-F2: Pin post-update Skill sync entry point (Hermes + OpenCode)
- Do not rely on bare `shutil.which("dyro")` alone
- Prefer updater-resolved bin, or `sys.executable -m dyro` / Scripts-next-to-prefix after `perform_update` documents the target
- Note: Hermes’ `sys.executable -m` alone is **insufficient** right after in-place upgrade if the running interpreter still loads the old package from memory; prefer the **new** install’s console script / re-exec. Pinning still beats PATH `which`.

### P1-F3: No-host setup honesty (Agy)
- No hosts → do not label option 1 `（推荐）`; default to defer
- Plan summary blocker-first (no optimistic “将创建镜像/分身” as if apply will succeed)

### P1-F4: Regression + boundary tests (OpenCode + Claude)
- P0 regression test (mandatory)
- `_refresh_skill_via_new_cli` success / non-zero / timeout / missing binary
- `cmd_update_now` calls refresh only when `perform_update` returns True
- At least one CLI test for `integration sync` upgrade-only / ABSENT no-op

## 5. P2 Follow-ups (non-blocking)

- Split `integration sync` no-op copy: ABSENT vs CURRENT
- Document or decouple `DYRO_NO_UPDATE_CHECK` vs startup Skill repair (Agy/Claude votes: keep command gate; prefer decouple env or document)
- README install vs sync one-liner; pt-BR `repararam` → `reparam`
- Conflict-state tests for `sync_managed_skill`
- Docs note: startup OUTDATED may migrate legacy installs (Claude P2-1)

## 6. Open Micro-Decisions — Arbitration

| # | Decision |
| --- | --- |
| 1 | **Keep** startup Skill sync on interactive `dyro` / `home` / `start` only. Prefer **document** `DYRO_NO_UPDATE_CHECK` coupling in this change; optional later decouple is P2. |
| 2 | Post-update Skill sync failure: **retry next launch**; do **not** fail the package update exit code. |
| 3 | `--install-skill` for non-interactive setup: **out of scope** this change. |

## 7. Instructions For The Execution Agent

1. Fix **P0-F1** in `cli.py` `main` / `_maybe_run_daily_update` wiring first.
2. Fix **P1-F2** entry-point resolution in `_refresh_skill_via_new_cli`.
3. Fix **P1-F1** + **P1-F3** setup UX honesty.
4. Add **P1-F4** tests; run focused suite: `tests/test_updates.py`, `tests/test_cli.py`, `tests/test_integrations.py`.
5. Do not touch user WIP plans (`plans/dyro-agent-bridge-*.md`).
6. Do not bump version / publish; keep CHANGELOG under Unreleased until P0/P1 closed.
7. Re-open board only if new P0 appears; otherwise mark P0-F1 closed in a short follow-up note under Final Arbitration.

## 8. Requires Human Verification

- Real multi-install PATH layouts (pipx vs uv tool vs venv) after `update now` — 须人工核 for P1-F2 completeness
- Hostless interactive setup UX after P1-F3 — 须人工核

Final signature: Cursor Root (parent agent)

---

## Follow-up (execution)

Time: 2026-08-12 Asia/Taipei

| Item | Status |
| --- | --- |
| P0-F1 same-turn stale overwrite | **closed** — `main()` skips `_maybe_sync_managed_skill` when `_maybe_run_daily_update()` returns True after refresh |
| P1-F1 setup completion honesty | **closed** — apply returns outcome; completion uses it |
| P1-F2 pin Skill sync entry point | **closed** — `_fresh_dyro_argv` (Scripts/bin beside `sys.executable`, else `-m dyro`) |
| P1-F3 no-host setup honesty | **closed** — defer default; blocker-first plan summary; install dry-run no longer pretends orphan mirror create |
| P1-F4 regression/boundary tests | **closed** — auto-patch skip sync; refresh argv/exit; update now wiring; sync CLI; setup UX |
| Bare `dyro update` ≡ check | **closed** (separate product ask, same WIP) |

Updated land posture: **Conditional Go → ready for focused re-test / land after green suite** (no remaining arbitration P0).
