# Prior boards extract — control-plane honesty line

Structured extraction from six cloud-agent transcripts. Verdicts and findings quoted from final assistant messages only. Closure claims recorded only where a later board/PR explicitly claimed them — not inferred.

---

## 1. board: main multi-seat (Grok) — original board before #59

| Field | Value |
| --- | --- |
| Transcript | `bc-cc8cd820-7e26-4e07-b847-020e1a48faf8` |
| SHA | `3e24897994cd6b44e9b6acbf82d2ab1210935b65` (`#57` OpenCode+Hermes avatars) |
| Production pin | tag `v0.7.10` = `78f8e6444fceb83e0e59f184c6020c1694988c83` |
| **Verdict** | **needs-work** |

### P0

| ID | file:symbol | Claim |
| --- | --- | --- |
| P0-1 | `src/dyro/integrations/manager.py:_host_home` + `_ensure_safe_directory` + `_install_avatars` | Advertised “no host-home creation” is inverted on the only commit ahead of `v0.7.10`: env or `host_homes` override skips existence checks, install then `mkdir`s missing path and `skills/`. |

### P1

| ID | file:symbol | Claim |
| --- | --- | --- |
| P1-1 | `src/dyro/cli.py:cmd_start`, `src/dyro/home.py:existing_line_workspace` | `dyro start` is a second door that proceeds on doctor FAIL when the only FAILs are missing-origin; `cmd_next` treats same class as `needs_repair`. |
| P1-2 | `src/dyro/workspace.py:is_missing_origin_finding` | Missing-origin classifier is a substring (`": missing origin/"`), not a parse; smuggleable through repo paths. |
| P1-3 | `docs/reviews/2026-08-19-slash-review-and-task-merge.md` lines 5 and 81 | Public review file leaks a non-fictional product-line name; identity tests do not scan `docs/`. |

### Claimed closed by a later PR/board

| Finding | Later claim | Source |
| --- | --- | --- |
| P0-1 (host-home mkdir) | **Closed on HEAD** `f62f00e4` — “Host-home mkdir under missing env/`host_homes`” | reboard §Closed on this HEAD (board 2) |
| P1-1 (`start`/`next` split) | **Closed on HEAD** `f62f00e4` — “`start` refuses every doctor FAIL; `open` skips only constructed missing-origin” | reboard §Closed on this HEAD (board 2) |
| P1-2 (substring classifier) | *(no later closure claim found in these six transcripts)* | — |
| P1-3 (public name leak) | *(no later closure claim found in these six transcripts)* | — |

---

## 2. reboard: main after #59/#60/#61 — found the P0 that became #62

| Field | Value |
| --- | --- |
| Transcript | `bc-ff297df0-020c-4568-8690-1104a068d208` |
| SHA | `f62f00e4bd43b890fa9a7f5a3d237899030260fb` (`origin/main`; includes `#57`, `#59`, `#61`, `#60`) |
| Prior same-line board | `origin/main` @ `3e24897` |
| **Verdict** | **needs-work** for a 0.7.11 tag |

Context (verbatim): “#59/#61 and the fold-twin / Console-copy parts of #60 hold. The leftover that blocks 0.7.11 is implicit/`--root` `next` ads keyed by **profile name**, which can resolve to a **different registered workspace** and even advertise `bootstrap --yes`.”

Cross-board close (later message): “Cross-board close at `f62f00e4`: **needs-work / No-Go for tagging 0.7.11.** … Seat A and Seat E called Go on honesty and host-home, but that does not hold for implicit next/briefing ads.”

### P0 (blocks 0.7.11)

| file:symbol | Claim |
| --- | --- |
| `src/dyro/cli.py:_briefing_command`, `src/dyro/continuation/ready_briefing.py:scoped_briefing_command`, `src/dyro/continuation/next_step.py:repair_commands` | Implicit or `--root` JSON `next` uses `config.name`. If that string uniquely fold-matches another registry alias, `next.commands` is a working selector for the other root. Live: `mutation_available: true` and `dyro --workspace test-workspace bootstrap --yes` against the non-default workspace. |

### P1 (leftover, not Go)

| file:symbol | Claim |
| --- | --- |
| `src/dyro/cli.py:_briefing_command` | Same keying fail-closes when the profile name is not registered (`workspace add --name` ≠ profile name). |
| `src/dyro/home.py:_run_config_home` | Prints doctor FAILs, then still `_print_ready_briefing`. |
| `src/dyro/home.py:open_task` / `src/dyro/tasks.py:existing_task_workspace` | Task launch skips the line-open doctor FAIL set. |
| `src/dyro/console/families.py:project_artifact` | Video `open_command` invents `dyro --workspace {alias} --dry-run line inbox` with no fold-collision guard (seat P0 demoted: `--dry-run`, fail-closed, not wrong-root). |
| `src/dyro/cli.py:_timeout_repair_commands` | Except path uses raw `briefing_command` / `unknown`. |
| `src/dyro/console/overview.py` single-workspace fetch | Exact-only alias; unique fold that CLI accepts is `WORKSPACE_NOT_FOUND`. |

### Closed on this HEAD (from this board — includes board 1 items)

| file:symbol | Claim |
| --- | --- |
| `src/dyro/integrations/manager.py:_host_home` | Host-home mkdir under missing env/`host_homes` — closed. |
| `src/dyro/cli.py:cmd_start`, `src/dyro/home.py:existing_line_workspace` | `start` refuses every doctor FAIL; `open` skips only constructed missing-origin — closed. |
| `src/dyro/cli.py:_control_plane_budget`, `_print_json_observation_timeout` | JSON doctor/status/next start at 45s; leftover is structured partial, not bare `kind=error` `DEADLINE_EXCEEDED` — closed (#61). |
| `src/dyro/hub.py:select_workspace_record`, `src/dyro/console/assets/app.js:recommendedCommand` | Unique fold + twin fail-closed; twin implicit `next` uses `--root`; Console copy does not invent `--workspace … doctor` — closed (#60). |

### Claimed closed by a later PR/board

| Finding | Later claim | Source |
| --- | --- | --- |
| P0 (implicit/`--root` retarget ads) | Happy-path **claimed P0 fix closed** on live path at `d84b742`; three residual paths still **needs-work** | review PR #62 (board 3) |
| P0 (implicit/`--root` retarget ads) | Residuals (1)(2)(3) **CLOSED**; **merge-ready** at `09d3234` | rereview PR #62 (board 4) |
| P1 `_timeout_repair_commands` except path | Addressed in #62 residual round (board 4 item 1) | rereview PR #62 (board 4) |
| Other P1s in this list | *(no explicit closure claims in these six transcripts)* | — |

---

## 3. review: PR #62 next root ads

| Field | Value |
| --- | --- |
| Transcript | `bc-4c0569e1-107d-4402-9064-d66933de8f3a` |
| SHA | `d84b742` on `cursor/workspace-next-root-ads-6ba8` |
| **Verdict** | **needs-work** |

Context (verbatim): “The happy-path P0 is real and closed. Implicit and `--root` JSON `next` no longer emit a working `--workspace` selector or `bootstrap --yes` for the other root. Deadline/repair still can, on shipped fallbacks the PR left in.”

### P0

| Status | file:symbol | Claim |
| --- | --- | --- |
| **Claimed closed (happy path)** | `cli.py:_briefing_command`, `next_step.py:next_commands` → `ready_briefing.py:scoped_briefing_command` → `hub.py:workspace_alias_retargets_root` | Implicit/`--root` JSON `next` returns `needs_repair` with only current-root ads (`--root` bootstrap/doctor); no `--workspace demo`/`Demo` for other root. |

### P1

*(Final message assigns no explicit P0/P1 IDs to residuals; listed as “Residuals that keep this from merge-ready”.)*

| # | file:symbol | Claim |
| --- | --- | --- |
| 1 | `cli.py:_timeout_repair_commands`, `cli.py:_print_json_observation_timeout` | Deadline exception fallback still emits a working fold selector (`--workspace Demo doctor` for other root) when `_config` fails after capture. |
| 2 | `ready_briefing.py:scoped_briefing_command`, `hub.py:workspace_alias_retargets_root` | Uncertain registry still fail-opens to `--workspace` (can include `bootstrap --yes`) when registry read fails. |
| 3 | `console/_inspect_worker.py:_unavailable_summary` → `overview.py:unavailable_workspace_summary` | Isolated timeout card helper still builds bad ad without `root=`; omit is backstop only. |

### Claimed closed by a later PR/board

| Finding | Later claim | Source |
| --- | --- | --- |
| Residual 1 (timeout after `_config` fail) | **CLOSED** | rereview PR #62 (board 4) |
| Residual 2 (registry unread fail-open) | **CLOSED** | rereview PR #62 (board 4) |
| Residual 3 (`_unavailable_summary` missing `root=`) | **CLOSED** | rereview PR #62 (board 4) |

---

## 4. rereview: PR #62 residuals

| Field | Value |
| --- | --- |
| Transcript | `bc-51d1c162-573b-47a4-aa87-d3a045a4817e` |
| SHA | `09d3234` on `cursor/workspace-next-root-ads-6ba8` |
| **Verdict** | **merge-ready** |

### P0

**None open.** Prior happy-path P0 from board 3 remains closed; three leftovers closed this round.

### P1

**None open** on JSON `next` / briefing / repair / timeout / registry-unread / unavailable-card doctor paths.

Closed this round (verbatim labels):

| # | file:symbol | Status |
| --- | --- | --- |
| 1 | `cli.py:_timeout_repair_commands`, `cli.py:_print_json_observation_timeout` | **CLOSED** — `_timeout_unscoped_repair_commands`; no `briefing_command(alias)` after `_config` fail. |
| 2 | `ready_briefing.py:scoped_briefing_command`, `hub.py:workspace_alias_retargets_root` | **CLOSED** — registry unread → `--root` for this workspace. |
| 3 | `console/_inspect_worker.py:_unavailable_summary` | **CLOSED** — forwards `root=`; production callers pass `root=record.root`. |

**Same-class leftover (not merge-blocking):** `console/families.py:project_artifact` and `console/assets/app.js` still format `--workspace ${alias}` for family/line copy — “pre-existing operator-copy, not this residual class.”

### Claimed closed by a later PR/board

*(This board is the closure record for board 3 residuals and reboard P0.)*

---

## 5. rereview: PR #61 honesty P1s

| Field | Value |
| --- | --- |
| Transcript | `bc-473d800c-dec0-4aca-a24c-a7e992720168` |
| SHA | `172824a` |
| **Verdict** | **merge-ready** |

### P0

**None.** “No residual bare `kind=error` `DEADLINE_EXCEEDED` on JSON `doctor` / `status` / `next`.”

### P1

**Open P0/P1:** none.

**Verified closed (claimed P1s):**

| # | file:symbol | Claim | Result |
| --- | --- | --- | --- |
| 1 | `cli._print_json_observation_timeout`, `cli._timeout_findings`, `cli._timeout_status_rows`, `cli._timeout_repair_commands` → `next_step.deadline_repair_commands` | Leftover reuses stash; `next` gets doctor repair | **Holds** |
| 2 | `next_step.next_commands`, `next_step.deadline_repair_commands` | `next_commands` DEADLINE → doctor, not `[]` | **Holds** |
| 3 | `cli.cmd_status`, `cli._print_json_observation_timeout` | JSON `status` partial exits 2; `--all` DEADLINE is partial/available | **Holds** |
| 4 | `CHANGELOG.md` Unreleased, `dyro-control-plane/SKILL.md` | Docs no Isolated Console 0.4s/scope fan-out | **Holds** |
| 5 | `cli._control_plane_budget` | 45s CLI JSON start kept | **Holds** |

**Residuals (P2, not merge-blocking):** `cli._timeout_workspace_name` / `cli._config` re-resolve on leftover; `cli._control_plane_resolution` written and never read; `cli.cmd_next` swallow exit 0 (payload still blocked).

### Claimed closed by a later PR/board

*(This board verifies #61; reboard board 2 cites #61 JSON budget as closed on `f62f00e4`.)*

---

## 6. rereview4: PR #60 console ads

| Field | Value |
| --- | --- |
| Transcript | `bc-ff071d1b-b01f-4828-aaab-505eaa4a9f79` |
| SHA | `67ad2ee` on `cursor/workspace-alias-casefold-a695`, merge-base `0d71a59` (`#61`) |
| **Verdict** | **merge-ready** |

### P0

**None.**

### P1

**Claimed P1s — hold:**

| # | file:symbol | Claim |
| --- | --- | --- |
| 1 | `app.js:recommendedCommand` | No longer invents `dyro --workspace ${alias} doctor` when backend left `recommendation.command` blank (FAIL+empty). Unique backend-supplied `--workspace` ads still copy. |
| 2 | `tests/test_console_operator.py` | Operator harness covers copy path (`test_fail_findings_and_empty_commands_are_not_unknown_or_bare`, `test_fold_twin_fail_empty_command_does_not_invent_workspace_doctor`). |
| 3 | `next_step.py:deadline_repair_commands` / `repair_commands` → `ready_briefing.py:scoped_briefing_command` | Rebase kept scoped ads and `#61` ReadBudget. |

**P2 leftovers (do not block):**

| file:symbol | Claim |
| --- | --- |
| `app.js:dryRunCommands`, channel retract `commandRow` | Still invent `dyro --workspace ${alias} --dry-run …` on family/channel panes. |
| `families.py:project_artifact` | Video `open_command` still stamps `--workspace {alias} --dry-run line inbox`. |

### Claimed closed by a later PR/board

*(Reboard board 2 cites fold-twin / Console-copy parts of #60 as closed on `f62f00e4`.)*

---

## Contradictions between boards

1. **Board 1 P0-1 vs reboard “Closed on this HEAD”** — Original board: host-home creation is **P0** inverted on `#57` delta. Reboard at `f62f00e4`: same item listed **closed** (after `#59`).
2. **Board 1 P1-1 vs reboard “Closed on this HEAD”** — Original board: `cmd_start` proceeds on missing-origin FAIL (**P1**). Reboard: **`start` refuses every doctor FAIL** — closed (after `#59`).
3. **Reboard seat split vs chair verdict** — Seats A/E called Go on honesty/host-home; chair **needs-work** because implicit/`--root` `next` retarget P0 overrides seat Go.
4. **PR #62 first review vs rereview** — Board 3: **needs-work** (happy-path P0 closed; three deadline/registry/unavailable residuals open). Board 4: **merge-ready** (same three residuals **CLOSED**).
5. **Original board vs reboard on retarget** — Board 1 at `3e24897` did **not** file the implicit/`--root` profile-name retarget P0; reboard at `f62f00e4` did — became **#62**.
6. **PR #61 `cmd_next` swallow exit 0** — Board 5: residual **P2**, “Not a ready lie”; scoped to `status`/`doctor` for claim 3 exit 2. Not contradicted elsewhere in these six transcripts.
7. **PR #60 P2 family copy vs reboard P1** — Reboard P1 lists `families.py:project_artifact` dry-run ad; PR #60 rereview4 labels same class **P2, do not block** — severity differs, not closure.

---

## Verdict summary

| Board | SHA | Verdict |
| --- | --- | --- |
| 1 original main | `3e24897` | needs-work |
| 2 reboard after #59/#60/#61 | `f62f00e4` | needs-work |
| 3 review PR #62 | `d84b742` | needs-work |
| 4 rereview PR #62 | `09d3234` | merge-ready |
| 5 rereview PR #61 | `172824a` | merge-ready |
| 6 rereview4 PR #60 | `67ad2ee` | merge-ready |
