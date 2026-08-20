# Line remote-ready design review

Date: 2026-08-20

Checkout: `6986f63de6bc65ee0a33cb6dc6d3fbe89368a22d` (`main`, package `0.7.6`)

Scope: verify D-01 through D-06 still exist on this tree, then attack the
intended `fix/line-remote-ready` design. That branch is not on this checkout
and is not on the GitHub default branch. This note reviews the design as
stated. It is not a patch, not Proof, and not `task review` PASS.

SSOT for current behavior: `src/dyro/workspace.py`, `src/dyro/cli.py`,
`src/dyro/hub.py`, `src/dyro/home.py`, `src/dyro/blueprint.py`,
`src/dyro/host/doctor.py`, `src/dyro/tasks.py`, and the tests named below.

Rules used here:

1. Cite files and functions. Do not invent counts, timings, or pass rates.
2. Source on this checkout beats the session description of a local branch.
3. Mark anything that cannot be proven from this tree as 须人工核.
4. Do not re-implement the patch. Do not bump the version.

## Verdict

**fix-the-design**

The six defects are still present on this `main`. The intended direction
(track `origin/<line.branch>` when that ref exists, otherwise create from the
declared base with `--no-track`; treat parent-tracking and a bad member of a
multi-repo line as not-ready; stop lying about `--workspace` paths; disclose
`allow_push=false`; fail-closed supervised apply when projections were never
compiled) is the right shape.

It is not implementable as written. Two holes would recreate false-ready or
false-not-ready on paths this tree already ships: join completion, and the
meaning of “upstream/HEAD is parent”. Fix those in the design before coding.

---

## 1. Defect verification on this checkout

All six still exist. None of the intended behavior is in this tree.

### D-01 — create never binds `origin/<line.branch>` — **still present**

`workspace.py:_plan_line_creation` plans `git worktree add`. When
`refs/heads/<line.branch>` is missing it adds `-b <line.branch>` and uses
`repo_base` (`line.base_for(repo_id)`) as the start-point. When the local
branch already exists it adds the existing branch name. There is no
`origin/<line.branch>`, no `--track`, and no `--no-track`.

```411:417:src/dyro/workspace.py
        command = ("worktree", "add")
        if branch_check.code != 0:
            command += ("-b", line.branch)
        command += (
            str(destination), line.branch if branch_check.code == 0 else repo_base
        )
        planned.append((repo_id, destination, command))
```

`create_line` then runs that tuple via `git(...)`. `preflight_line` calls the
same planner. Home (`home.py` wizard, branch `feat/<id>` or `hotfix/<id>`) and
CLI (`cli.py:_create_line`) both go through `create_line`. Join
(`blueprint.py:_ensure_line`) does too.

If `repo_base` is itself a remote-tracking name such as `origin/main`, Git may
set the new branch’s upstream to that parent. This tree never asks for
`origin/feat/<child>` / `origin/<line.branch>`.

`anchor-reference` repos skip `worktree add` entirely (empty command tuple,
symlink to the anchor).

### D-02 — doctor / next treat a parent-tracking or origin-less line as ready — **still present**

`workspace.py:doctor` walks each line/repo and checks, in order:

1. worktree exists and is a Git repo
2. `git branch --show-current` equals `line.branch`
3. `anchor-reference`: symlink target is the configured anchor
4. `linked-worktree`: not a symlink, and
   `rev-parse --git-common-dir` matches the anchor

It never reads `origin/<line.branch>`, `@{upstream}`, or whether HEAD’s
upstream is the parent base. A missing remote feat branch and a branch that
tracks `origin/main` while `line.branch` is `feat/<id>` both PASS if the four
checks above hold.

`cli.py:cmd_next` loads findings from that same `doctor`, keeps strings that
start with `FAIL`, and if that list is empty continues into `needs_line` /
`needs_agent` / `ready`. Zero FAIL is ready. `cli.py:cmd_doctor` uses the same
FAIL list for exit status, so `dyro doctor` also PASSes these cases.

The only next exception is bootstrap: FAIL set equal to “missing or not Git”
for repos that have a remote and a safe destination. Remote-feat / parent
tracking are not in that set, and today they are not FAILs at all.

### D-03 — `--workspace /abs/path` hits the alias validator; text `next` maps `ValidationError` to missing workspace / exit 0 — **still present**

`--workspace` is `dest="workspace_alias"` (`cli.py:_add_common`). It is
mutually exclusive with `--root`.

Text `_config` resolves an alias with `hub.py:get_workspace`, which starts at
`config.py:validate_id`. The pattern is
`SAFE_ID = ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$`. An absolute path contains `/`
and raises `ValidationError` (`工作区别名 只能包含字母、数字、点、下划线和连字符`).

JSON `_config` uses `resolve_workspace_readonly`, which also calls
`validate_id(workspace, "工作区别名")` before registry lookup
(`continuation/resolution.py`).

`cli.py:cmd_next` then does:

```2459:2477:src/dyro/cli.py
    except ValidationError:
        if args.format == "json" and (
            getattr(args, "workspace_alias", None) or getattr(args, "root", None)
        ):
            raise
        if args.format == "json":
            _print_control_plane_json(
                "next_step",
                state="workspace_missing",
                ...
            )
            return
        print("尚未发现 Dyro 工作区。")
        print("加入团队项目：dyro join <蓝图地址>")
        print("设置一个新项目：dyro setup")
        return
```

Text mode: any `ValidationError` from `_config` becomes “尚未发现工作区” and
returns (process exit 0). That includes `--workspace /abs/path`. It also
includes a valid alias whose `load(root)` later raises `ValidationError` for
a bad Profile — same lie, different cause.

JSON mode with `--workspace` or `--root` set re-raises. `ValidationError` is a
`DyroError` (`errors.py`), so `main` exits 2. The D-03 swallow is text `next`.

There is no “use `--root`” message on this path.

### D-04 — alias match is exact and case-sensitive; no close-match hint — **still present**

`get_workspace` does `record.name == name`.
`resolve_workspace_readonly` does the same after `validate_id`.
`resolve_workspace` (interactive) also uses `record.name == workspace`.

A wrong-case alias that still matches `SAFE_ID` misses, then:

```307:311:src/dyro/hub.py
        raise DyroError(
            f"未登记工作区：{name}；运行 dyro workspace list 查看可用项目"
        ) from exc
```

That points at `workspace list`. It does not list close names. JSON readonly
resolution becomes `WORKSPACE_NOT_REGISTERED` with no suggestions.

### D-05 — `allow_push=false` only gates `task merge --push`; status / next do not disclose raw `git push` — **still present**

The only runtime gate found:

```2806:2808:src/dyro/tasks.py
    if push and not config.policy.allow_push:
        raise DyroError(
            "当前 Profile 禁止 push；请在 dyro.toml 的 policy.allow_push 显式开启"
```

`home.py:print_status` / `cli.py:_status_payload` print `status_rows`
(scope, repo, branch, HEAD, dirty, upstream). They do not mention
`policy.allow_push`. `cmd_next` ready text is “工作区已就绪” / a briefing.
No status or next path says that `git push` from the worktree still works.

Default fixtures set `allow_push = false` (`tests/support.py`, onboarding
template, architecture sample). That default is policy, not a Git hook.

### D-06 — supervised apply fail-open when projections were never compiled — **still present**

```83:87:src/dyro/host/doctor.py
def assert_projections_allow_mutation(config: Config) -> None:
    """Block the next mutation tick only when a compiled projection is stale."""
    report = inspect_projections(config, user=False)
    if not report.compiled:
        return
```

`inspect_projections` sets `compiled = bool(manifests) or bool(orphans)`.
No `*.toml` and no orphan skill/hook directory ⇒ `compiled=False`,
`findings=[]`, `ok=True`. `render_doctor_text` prints
`未编译宿主投影` without FAIL. `cmd_host_doctor` raises only when
`not report.ok`, so never-compiled host doctor does not FAIL. That part of
the intended split already matches ordinary host doctor.

`continuation/supervision.py:apply_supervised_wave` calls
`assert_projections_allow_mutation` before the owner lease. Never-compiled
therefore applies. Encoded by `tests/test_host.py:test_never_compiled_does_not_block_apply`.

---

## 2. Intended design — attack

The local branch is not here. The following is an attack on the written
intent, using this tree as the integration surface it would land on.

### 2.1 Join / blueprint (P0 if left unspecified)

`blueprint.py:apply_join_plan` clones each anchor at a **pinned full SHA**
(`git clone --no-checkout`, detach at that object), then `_ensure_line` →
`create_line`, then:

```779:782:src/dyro/blueprint.py
        findings = doctor(config)
        failures = [finding for finding in findings if finding.startswith("FAIL")]
        if failures:
            raise DyroError("join 完成后 doctor 仍发现结构错误：\n" + "\n".join(failures))
```

Blueprint lines name a branch (`feat/…`, `release/…`, or anything
`_git_branch` accepts). Bases are immutable object IDs, not “this branch
already exists on the remotes”. `docs/workspace-blueprints.md` describes
exactly that contract.

If doctor FAILs when `origin/<line.branch>` is missing after clone:

- a shared line whose branch exists on the remotes: clone fetches refs
  (default clone is not `--single-branch` here), intended create tracks
  `origin/<line.branch>`, join doctor can PASS. That path is fine.
- a SHA-pinned line whose branch is **not** on the remotes (new line, or
  branch only created later): intended create uses `--no-track` from the
  SHA, doctor FAILs missing-origin, join raises **after** Profile, clones,
  and worktrees already exist. Join state is not marked complete.

The intended note says home-open ignores missing-origin. It does **not**
say join completion does. That is a mutation-then-fail hole on a shipped
command. Pick one and write it down:

1. Join completion uses the same missing-origin ignore as home-open; or
2. Join requires `origin/<line.branch>` after clone (and the blueprint
   contract changes: advertised branch must exist on every remote); or
3. Join’s post-create doctor keeps today’s structural checks only.

Until one of those is chosen, do not implement.

`_clone_anchor` does not `git fetch` on resume. A retry against an already
cloned anchor uses `_existing_anchor_matches` (origin URL, clean, detached
HEAD == SHA). Stale or absent `origin/<line.branch>` on that anchor is the
same no-fetch problem as §2.3.

Hotfix is `kind="hotfix"` and is not a blueprint join kind
(`_line_from_blueprint` hardcodes `kind="line"`). Join will not create
hotfixes. The join hole is line-only.

### 2.2 “upstream/HEAD is parent” (P0 if read as HEAD SHA)

Intended doctor FAILs when “upstream/HEAD is parent”. A line just created
from `repo_base` with `--no-track` has:

- no `@{upstream}`
- `HEAD` commit equal to the parent base (no line commits yet)

If “HEAD is parent” means `rev-parse HEAD` equals `rev-parse <line.base>`,
**every** new local line FAILs that check. Home-open is specified to ignore
**only** missing-origin. Create-then-open in `home.py:_run_config_home`
(new-line / new-hotfix → `existing_line_workspace`) would still block.
That contradicts “a new local line can still be entered”.

Write the predicate as:

- FAIL missing-origin: `refs/remotes/origin/<line.branch>` absent
  (or whatever exact ref name you choose), per repo
- FAIL parent-tracking: `@{upstream}` resolves and equals the parent
  (`line.base_for(repo)` or `origin/<basename>` of that base), **not**
  “HEAD SHA equals base SHA”
- no-upstream + missing-origin: only the missing-origin FAIL (home-open
  ignore applies; next stays not-ready if you keep that product choice)

Do not FAIL merely because the line has not diverged from its base.

### 2.3 Stale remote-tracking refs, no fetch (P1)

Intended create and doctor look at whether `origin/<line.branch>` **exists
in the local remote-tracking namespace**. Nothing in the intended note
fetches.

- Remote branch deleted, local `origin/<line.branch>` still present:
  create tracks a ghost; doctor PASSes; next can say ready. False-ready.
- Remote branch created after last fetch: doctor FAILs missing-origin;
  next not-ready. False-not-ready.
- Remote force-pushed: worktree add tracks the stale tip.

`_plan_line_creation` and `doctor` today are local Git reads
(`show-ref`, `rev-parse`). Adding an implicit `git fetch` there would be a
network side effect on `doctor` / `next` / home-open, which those commands
currently do not do.

Accept “we trust the last fetched remote-tracking refs” in the design, and
do not call that “remote exists”. Or add an explicit, optional refresh
with a distinct finding (`origin/<branch>` missing **locally**, not
“remote confirmed absent”). Do not have `next` state `ready` imply a
live remote check.

### 2.4 Home-open exception width (P1)

Today `home.py:existing_line_workspace` treats **all** doctor FAILs whose
prefix is `FAIL repository <repo>:` or `FAIL <kind>:<id>/` as blocking:

```742:756:src/dyro/home.py
def existing_line_workspace(
    config: Config, line_id: str, kind: str | None
) -> tuple[Line, Path]:
    line = get_line(config, line_id, kind)
    relevant = {f"FAIL repository {repo_id}:" for repo_id in line.repositories}
    relevant.add(f"FAIL {line.kind}:{line.id}/")
    failures = [
        finding
        for finding in doctor(config)
        if any(finding.startswith(prefix) for prefix in relevant)
    ]
    if failures:
        raise DyroError(
            f"{line.kind} {line.id} 尚未就绪：{failures[0]}。下一步：dyro doctor"
        )
```

Callers: `open_line`, `cli.py:cmd_start`, and home after the user picks a
line / just created one. The intended ignore is “home open only”. If it is
implemented only in the home menu, `dyro start` / `dyro open` still block a
new local line. If it is implemented inside `existing_line_workspace`,
start and open get the same exception. Say which.

Too-wide implementations to reject:

- ignore every `FAIL <kind>:<id>/…` when any of them mentions origin
- ignore by substring `origin` or `remote` (parent-tracking messages will
  mention `origin/main`)
- ignore the whole line when one repo is missing origin and another tracks
  the parent

Required: ignore **only** the missing-origin finding code/string. Wrong
branch, parent-tracking, common-dir, symlink, and missing worktree still
block. One bad repo still blocks the line (agrees with “one repo in a
multi-repo line is wrong”).

`existing_task_workspace` (`tasks.py`) does **not** call `doctor`. Opening a
task checks the task worktree branch and common-dir only. A parent-tracking
**line** can still be entered through a task. If remote-ready is a line
invariant, say whether task open inherits it. Today it would not, and the
intended note does not mention tasks.

### 2.5 Hotfix and non-`feat/` branches (P1)

`_plan_line_creation` and `doctor` are kind-agnostic. They use
`line.branch`. CLI/home default `hotfix/<id>` and require an explicit
`--base` for hotfixes. Blueprint examples also use `release/…`.

Intended create text says `origin/<line.branch>` (correct). Intended doctor
text says “remote feat missing” and the defect statement says
`origin/feat/<line>`. If the implementation hardcodes `feat/` or
`origin/feat/<id>`:

- hotfixes never match (always FAIL or never checked)
- a line whose `branch` is not `feat/<id>` is mis-diagnosed

The check must be `origin/<line.branch>` (and local `refs/heads/<line.branch>`),
for both `kind=line` and `kind=hotfix`. Home-open’s missing-origin ignore
must use the same ref, not a `feat/`-only pattern.

A new hotfix from a release SHA with `--no-track` is the same shape as a
new feat line: missing-origin FAIL, next not-ready, home-open allowed if
and only if parent-tracking is not also FAIL (§2.2).

### 2.6 `next` ready vs remote-ready (P1)

`cmd_next` is documented as one safe next step so a newcomer can start.
Today `state=ready` means “zero doctor FAIL, has a line, has an agent”.
Intended: missing origin ⇒ FAIL ⇒ `needs_repair` and the text
“工作区还不能开始任务”.

That is the opposite lie of D-02: a healthy local-only line becomes
“cannot start”. Repair commands stay empty unless the FAIL set equals the
bootstrap predicate. The user is pointed at `dyro doctor` for a condition
that is expected for a brand-new `--no-track` line.

If the product name is remote-ready, do not reuse `needs_repair` /
“还不能开始任务” for missing-origin-only. Give it a distinct state
(example: `needs_remote` / disclose “local line, origin/<branch> absent”)
and keep `ready` for “you can enter and work”. Or keep next strict and
change the sentence so it does not say the workspace cannot start tasks.

Home already prints “检测到 N 个结构问题；只会阻止进入受影响的目标”
for any doctor FAIL. After this change, every new local line would show
that banner even when open is allowed. Tighten that copy the same way.

### 2.7 `anchor-reference` (P1)

Intended create talks about `worktree add`. `anchor-reference` never calls
it. Intended doctor talks about origin and parent-tracking per repo.

If origin checks are skipped for `anchor-reference`, a multi-repo line can
be next-ready while the referenced anchor has no `origin/<line.branch>` or
tracks the parent. If they are applied, a reuse-anchor line used only
locally FAILs the same as linked worktrees.

Specify per storage mode. Today doctor already special-cases
`anchor-reference` and stops after the symlink checks.

### 2.8 Tests that still encode the old predicate (P1 — implementation trap)

These assertions on **this** tree will keep the old meaning of ready /
fail-open if they are left green by weakening the product instead of
updating the tests.

Doctor / create (zero FAIL after a local `create_line` with no remotes):

- `tests/test_workspace.py:test_create_line_and_dynamic_doctor`
- `tests/test_workspace.py:test_anchor_reference_storage_is_explicit_and_doctor_validates_it`

`next` `state=ready` after `create_line` with no `origin/feat/…`:

- `tests/test_cli.py:ObjectiveCliTests.test_control_plane_next_preserves_an_explicit_workspace_selector`
- `tests/test_cli.py:ObjectiveCliTests.test_next_with_one_live_objective_points_to_follow_up`
- sibling next tests in that class that assume doctor is clean

Start / open going through `existing_line_workspace` after a local create:

- `tests/test_cli.py:ProfileCommandsTests.test_start_can_launch_an_installed_tool_without_a_profile_adapter`
- `tests/test_cli.py:StartTests` (creates `feat/alpha` then starts)

Supervised apply fail-open (must invert if apply becomes fail-closed):

- `tests/test_host.py:test_never_compiled_does_not_block_apply`

Worktree argv shape (`-b` at `args[2]`, destination at `args[4]`):

- `tests/test_workspace.py:test_create_line_rolls_back_when_a_later_repository_fails`
  (`flaky_git`). Adding `--no-track` / `--track` shifts indices.

`verify_changeset` does **not** call `workspace.doctor` and will not start
FAILING just because origin is missing. Do not “fix” changeset tests for
this change; they are a different predicate
(`tests/test_changesets.py`).

`cmd_next` text `ValidationError` → missing workspace has no direct test
found. Add a test that `--workspace /abs/path` tells the user to use
`--root` and does not exit 0 with “尚未发现工作区”. Do not keep a test
that treats that path as `workspace_missing`.

`test_never_compiled_does_not_block_apply` is the current contract comment
in `assert_projections_allow_mutation`. Inverting it is part of D-06, not
an accidental break. `hosts_to_compile` always includes at least
`DEFAULT_HOST` (`cli`), so “run `host compile` once” is a finite
requirement, not an empty-host deadlock. Still a behavior break for every
workspace that never compiled: `objective apply` would stop until compile.
Write that in the same design note. Ordinary `host doctor` / workspace
`doctor` must stay non-FAIL when nothing was compiled (already true for
host doctor; workspace doctor never inspects projections).

### 2.9 D-03 / D-04 leftovers if the patch is too narrow (P1 / P2)

Path-as-`--workspace` → “use `--root`” is correct. That is not enough:

- Text `next` must stop mapping **all** `ValidationError` to missing
  workspace. A valid alias + invalid Profile is not “尚未发现工作区”.
- JSON `next` already re-raises when `--workspace` / `--root` is set.
  Keep that. Do not teach JSON the text lie.
- Close-match hints: stay exact/case-sensitive (agreed). Suggest from the
  registry name list only. Do not resolve the wrong workspace. Do not
  treat a filesystem path as an alias just because it is close.

### 2.10 D-05 disclosure (P2, design OK)

Disclosing `allow_push=false` on status/next, without claiming a hook, is
correct. Do not add a Git hook in this change. Do not let next print
“push is disabled” as if `git push` will fail. The only enforced gate
remains `merge_task(..., push=True)`.

### 2.11 D-06 fail-closed apply (P1 product, design OK if scoped)

Fail-closed when `not report.compiled` is the opposite of the current
docstring and of `test_never_compiled_does_not_block_apply`. It is
justified for supervised apply. Keep:

- `inspect_projections`: never-compiled ⇒ `compiled=False`, no FAIL
  findings
- `cmd_host_doctor`: no DyroError merely because nothing was compiled
- workspace `doctor`: still no projection checks
- `assert_projections_allow_mutation`: raise when `not compiled` **or**
  `not ok`

Stale compiled already fail-closes (`test_host.py` orphan / tamper cases).
Do not change those.

---

## 3. P0 / P1 / P2

### P0 — fix in the design before coding

1. **Join completion vs missing-origin FAIL.**
   `apply_join_plan` currently treats any doctor FAIL as join failure after
   mutation. SHA-pinned blueprint lines are not required to have
   `origin/<line.branch>`. Choose ignore / require-on-remote / structural-
   only for this path and write it down.

2. **Define parent-tracking as `@{upstream}`, not HEAD SHA == base.**
   Otherwise home-open’s “ignore only missing-origin” cannot open a new
   `--no-track` line, including the home create-then-open flow.

### P1

3. **No fetch.** Remote-tracking existence is not live remote existence.
   Do not let `next` `ready` mean the latter. Document or add a distinct
   finding.

4. **`next` copy / state.** Do not reuse `needs_repair` +
   “工作区还不能开始任务” for missing-origin-only.

5. **Home-open exception surface.** State whether `existing_line_workspace`
   / `start` / `open` / task open share the ignore. Ignore by exact
   finding, not by “origin” substring. One parent-tracking repo still
   blocks.

6. **Use `origin/<line.branch>` for hotfix and non-`feat/` names.**
   Do not hardcode `feat/`.

7. **`anchor-reference`.** Say whether origin / upstream checks apply.

8. **Rewrite tests that encode the old predicate** listed in §2.8.
   Invert `test_never_compiled_does_not_block_apply`. Fix `flaky_git`
   argv indexing. Do not “preserve” `next` `ready` by dropping the new
   FAILs.

9. **`cmd_next` `ValidationError` handler** must not keep the blanket
   text-mode mapping after the path/`--root` hint is added.

10. **Supervised apply fail-closed** is a behavior break for never-compiled
    workspaces. Same patch must say `host compile` is required before
    `objective apply`.

### P2

11. Close-match hints on wrong-case aliases (strict match stays).
12. `allow_push=false` disclosure only; raw `git push` remains possible.
13. Task-open leftover: line remote-ready is not checked on
    `existing_task_workspace` unless the design adds it.

---

## 4. What would be design-ok after the P0s

If P0-1 and P0-2 are written into the intended note, the rest of the
intent is acceptable:

| Intent | Review |
| --- | --- |
| Track `origin/<line.branch>` when that ref exists; else `-b` from `repo_base` with `--no-track` | Correct for D-01. Apply per repo. Existing local branch: say whether you only set upstream or refuse to attach. |
| Doctor FAIL missing remote-tracking line branch, or `@{upstream}` is parent, or any member repo is wrong; `next` not ready | Correct for D-02 if “not ready” is not the current `needs_repair` sentence. |
| Home-open ignores only missing-origin | Correct for new local lines, once parent-tracking ≠ HEAD SHA. |
| Path `--workspace` → use `--root`; do not map that `ValidationError` to missing workspace / exit 0 | Correct for D-03. |
| Strict alias + close-match suggestions | Correct for D-04. |
| Disclose `allow_push=false` on status/next; no hook claim | Correct for D-05. |
| Apply fail-closed if not compiled; ordinary doctor does not FAIL for never-compiled | Correct for D-06. |

---

## 5. Out of scope / 须人工核

- The local unpushed `fix/line-remote-ready` branch was not available on
  this checkout. Whether that branch already chose a join rule or a
  parent-tracking definition is 须人工核 against the author’s tree.
- No runtime metrics were collected. No test suite was run for this
  review-only pass.
- This review does not authorize implementation, merge, or a version bump.

## 6. Go / No-Go for implementation

| Object | Conclusion |
| --- | --- |
| Confirm D-01…D-06 on `6986f63` / `0.7.6` | Go — all six still present |
| Implement the intended patch as written | No-Go until P0-1 and P0-2 are specified |
| Design after those two sentences exist | Conditional Go — land P1 in the same change, especially tests and `next` wording |
