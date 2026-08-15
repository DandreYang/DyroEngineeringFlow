# 交付物理学实现对抗式复核审查委员会

日期：2026-08-15

范围：

- 仓库：`/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow`
- 模块：已落地的 `0.7` Proof / 衰减、`0.8` Capability / Console、`0.9` Host Compiler、`1.0` `verify-bundle`
- 基线：已发布 `0.6.0` 语义；本树实现尚未升版本号

审查材料：

- 实现：`src/dyro/proof/`、`src/dyro/capability/`、`src/dyro/host/`、`src/dyro/cli.py`、`src/dyro/tasks.py`、`src/dyro/graph.py`、`src/dyro/continuation/`、`src/dyro/console/read_model.py`
- 测试：`tests/test_proof_*.py`、`tests/test_capability.py`、`tests/test_host.py`、`tests/test_readme_identity.py`、`tests/test_release_gates.py`
- 发布：`.github/workflows/ci.yml`、`.github/workflows/pypi-publish.yml`、`tools/verify_bundle_stranger.py`、`tools/verify_release_gates.py`
- 叙事：`README.md` 与各语言 README、`pyproject.toml`（仍为 `0.6.0`）

SSOT（源码优先于文档与先前评审）：

- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/src/dyro/tasks.py`
- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/src/dyro/proof/bundle.py`
- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/src/dyro/proof/evaluate.py`
- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/src/dyro/host/doctor.py`
- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/src/dyro/capability/probe.py`
- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/plans/delivery-physics-implementation.md`
- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/docs/superpowers/reviews/2026-08-15-delivery-physics-adversarial-review-board.md`（设计评审；本轮审实现，不重开已锁决策，除非源码证伪）

模式：Code review mode。材料是已落地实现，不是待批设计。

固定决策（除非源码证明其错误，否则不得重开）：

- 产品身份：本地优先的多仓交付物理引擎。
- 权威投影 B：必编译 skill；仅当 hook 表面被证明时再投影 deny hook；hook 不是 OS 隔离。
- **A1**：衰减是现有 merge / 下游绑定检查的投影。`merge_task` / `check_dispatchable` 不读 Proof store。
- **B1**：`verify-bundle` 核完整性，不核身份，不承诺与当前工作区 `verify` / merge 同一套 `live` / `decayed`。
- 任务仓 dirty：保持 `0.6` 拒绝。
- `ContinuationSnapshot` 是死类型。
- 不把 `tools.json` / PATH 当已审计 Card。
- 不把 hook 写成沙箱。
- 不在产品面写对标附录。

开放微决策（请表态，不要另开产品线）：

1. deny hook 只写在 `.dyro/host-projections/`，不写入 Card 声明的 `hook_surface`，是否构成「已投影但宿主读不到」的假权威？
2. 包版本仍是 `0.6.0`，但 `verify-bundle` 与 README 身份句已按 `1.0` 落地，对外是否构成版本谎言？
3. 单/`多` `--git-dir` 是否足够覆盖多仓对象库，还是必须在 1.0 叙事里写明「调用方负责备齐对象」？

## 规则

1. 审查员只写自己的签名章节。他人不得改写、缩写或润色该章。
2. 源码、现有 schema、现有测试优先于设计稿和计划。
3. 无法从源码或所供材料证明的主张标 `须人工核`。
4. 发现使用 P0 / P1 / P2。
5. 本轮以缺陷、回归、安全、契约破裂、缺失测试为先；风格问题除非造成可测风险，否则不报。

---

# Cursor Review Section

Reviewer: Cursor（主控独立核源，不继承其他席位）
Time: 2026-08-15
Verdict: Conditional Go

## Contract Consistency

A1 在 merge / dispatch 主路径上成立：`tasks.py` 的 `merge_task`（2058–2068）与 `check_dispatchable`（545–556）只调用 `_valid_review_acceptance` / `_valid_external_signoff` / `_assert_dependency_integrated`，全文件无 `list_proofs` / `evaluate_proofs` 导入。`PROOF_DECAYED` 只出现在既有拒绝的人话里（2063–2067）。

P5：`graph.py` `_explain_with_config`（195–198）对 `done` 依赖调用 `_assert_dependency_integrated`。

P4：`SchedulerSnapshot.decayed_merge_subjects`（snapshot.py:70）在 `inspect_proofs` 为真时填充（280–285）；`_payload`（83–93）不含该字段，digest 不因 Proof 检查而变。`live_merge_evidence` 只在测试中拼进 `ProgressFacts`，生产 `BudgetUsage` 未接线。`ContinuationSnapshot` 仍是死类型。

B1：`verify_bundle`（bundle.py:63–119）默认不做 workspace decay；无 `--current-heads` 时 `_integrity_of` 只返回 `live` 或 `inconclusive`（184–185）。CLI JSON `mode=integrity`。`task evidence` ZIP 无 `manifest.json` 时走 `NOT_PROOF_BUNDLE`。

P7：`observations.py` Console 采样 `inspect_integration=False`（151, 284），因此 `inspect_proofs` 默认亦为 False；`proof_inspection` 恒为 `not_inspected`（208, 358）。

P10：`_adapter_argv` 在 `mode=="write"` 且 Card `intents` 无 `execute` 时拒绝（tasks.py:1180–1181）。

P12：`apply_supervised_wave` 在取 OwnerLease 之前调用 `assert_projections_allow_mutation`（supervision.py:390–392）。无 manifest 时 `compiled=False` 放行（doctor.py:73–75）。

## Source Evidence Accuracy

已用当前源码核对，不是计划转述。

## Decision Validity

锁定决策未被实现推翻。开放微决策的源码事实：

1. deny hook 只写 `host-projections/<host>/deny-hook.json`（compile.py:336–339）。`hook_surface` 只作存在性证明（probe.py:64–76）。宿主进程不会自动加载该文件。
2. `pyproject.toml` version 仍为 `0.6.0`。`verify-bundle` 与 README 身份句已按 1.0 契约落地。`verify_release_gates.py` 仅在 tag/version 为 1.0.0 时强制门禁。
3. `--git-dir` 可重复；对象在任一库可解析即可。无 pin 的 Proof（如仅有 `bytes_sha256` 的 `action_receipt`）在缺 `--git-dir` 时仍可 `live`（bundle.py:178–185）。

## Plan Executability

P1–P13 主路径有测试。缺口见 Required Fixes。

## Scope And Risk

- 完整性 `live` 被误读成「现在能 merge」：CLI 有 `mode=integrity` 与帮助文案，README 身份句未解释两套结论。
- Host doctor 只挡 `objective apply`，不挡 `task run` / `task merge`。与 P12「mutation tick / ActivationLease」字面一致，但比「所有 mutation」窄。
- `[[capabilities]]` 无 launch 的 Card 在 `test_capability` 中永不可执行（probe.py:46–50），host 可用表只含 adapter 探测通过者。

## Go/No-Go

Conditional Go：可以当 0.7–0.9 实现与 1.0 **能力**收口，但在升 `1.0.0` 版本号之前必须关闭下列 P1（P0 未看到会改 0.6 merge 对错的破裂）。不得在当前 `0.6.0` 包号下对外宣称已发布 1.0。

## Required Fixes

### P1 · 无 git pin 的 bundle 在缺 `--git-dir` 时可以 `live`

证据：`bundle.py` `_integrity_of` 仅当 `shas` 非空才要求 `git_dirs`。`action_receipt` 常无 `repo_heads`。

决策：1.0 叙事写「必须提供 git 对象」。缺 `--git-dir` 时整捆不得 `live`（全部 `inconclusive`），或 CLI 无 `--git-dir` 直接拒绝。

验收：只含 `action_receipt` 的 bundle，无 `--git-dir` → 退出码 3，无 `live`。

### P1 · deny hook 未投影到已证明表面

证据：compile 只写投影树；`hook_surface` 不被写入。

决策：产品上二选一并写进 CLI/doctor：要么声明「hook 是可搬运的策略文件，宿主需自己指向投影路径」；要么在表面是目录时写入 `hook_surface/dyro-deny.json`。不得暗示宿主已经在拦截 `integrate`/`publish`。

### P1 · 版本号与 1.0 能力不同步

证据：`pyproject.toml` `0.6.0`；README 已锁身份句；`verify-bundle` 已是硬门禁。

决策：对外发布前升到与列车一致的版本，或在 README 标明「实现已含 1.0 核验能力，包版本未升」。禁止用 0.6.0 轮子暗示 1.0 已发布。

### P2 · bundle 级失败伪造成 `review_verdict`

证据：`_bundle_inconclusive`（bundle.py:276–287）kind 固定为 `REVIEW_VERDICT`。

决策：使用明确的 bundle 失败 subject/kind 展示，避免 JSON 消费者以为存在一条复核 Proof。

### P2 · Host doctor 不管 `task run`/`merge`

证据：仅 `apply_supervised_wave` 调用 `assert_projections_allow_mutation`。

决策：若产品要「手改投影不能偷偷继续自动跑」，写明范围是受监督 apply；或把同一检查扩到其他 mutation 入口。

---


# Code Reviewer Review Section

Reviewer: code-reviewer  
Time: 2026-08-15  
Verdict: Conditional Go

## Contract Consistency

A1 holds on the merge / downstream write path. `tasks.py` does not import `dyro.proof`. `merge_task` (2058–2068) still only calls `_valid_review_acceptance`, optional `_valid_external_signoff`, then `_prepare_merge`. `check_dispatchable` (545–556) still only walks decisions, `done`, and `_assert_dependency_integrated`. `PROOF_DECAYED` is extra wording on those same rejects, plus planner attention (`planner.py` 272–281). `build_task_readiness` (70–176) does not read `decayed_merge_subjects`. Torn `review.md` still leaves downstream dispatchable when the ancestor holds (`test_proof_decay.py` 358–366). Line dirty stays `_prepare_merge`’s “开发线仓库不干净”, not `PROOF_DECAYED` (303–313).

B1 is split correctly at the CLI: workspace `verify` is `mode=rebind`; `verify-bundle` is `mode=integrity` (`cli.py` 1919–1951). No `--current-heads` cannot emit `decayed` (`bundle.py` 183–184; `test_proof_cli.py` 141–145, 208–229). Evidence ZIP and in-zip git layouts are inconclusive (`bundle.py` 80–89, 230–237).

The 1.0 integrity contract is not closed. `_valid_manifest` (224–227) only checks `kind` + `schema_version`. `verify_bundle` (99–102) skips the member digest when `proof_sha256[id]` is missing or empty, so a rewritten `proofs/*.json` can still become `live` if procedure / substrate / caller objects pass. That is not “integrity of Proof Bundle + caller git objects”.

`★ Insight ─────────────────────────────────────`  
A1 is a *projection* rule: merge keeps the 0.6 predicates; Proof only names the same False. B1 is a *different* predicate: zip bytes + caller objects. The bug is treating a present JSON member as attested when the manifest no longer binds its digest.  
`─────────────────────────────────────────────────`

## Source Evidence Accuracy

Derive identity omits clock/mtime (`models.py` 68–70; `test_proof_derive.py` 138–145). `list_proofs --task` excludes `action_receipt` (`derive.py` 34–40). `contract_hash` is attempt `task_contract_sha256` vs Objective `contract_sha256` (`derive.py` 120, 449–454). `ContinuationSnapshot` is still a dead type with no `proofs[]` (`models.py` 268–278). `SchedulerSnapshot._payload` (83–128) does not include `decayed_merge_subjects`, so the digest does not become a second PASS. Console summary hard-codes `proof_inspection=not_inspected` (`observations.py` 208) and adds no Proof git I/O.

`force_inconclusive` is a no-op: both branches set `INCONCLUSIVE` (`derive.py` 374). Evaluate then overwrites from predicates that return `False` rather than `None` for missing/unparseable files (`evaluate.py` 49–50, 98–102; `_valid_review_acceptance` 1137–1147 returns `False` on missing receipt/review). Plan/ADR: 缺文件 / 不可解析 → `inconclusive`. Tests only assert derive-time inconclusive (`test_proof_derive.py` 130–136); after `list`/`verify` the same torn review is `decayed` (`test_proof_decay.py` 281–287). Does not forge `live`. Does not change merge truth.

`_integrity_of` requires `--git-dir` only when `repo_heads` contain SHAs (`bundle.py` 177–184). CLI help says `--git-dir` 缺省不得报 live (`cli.py` 2838–2842). A proof with procedure + `bytes_sha256` and empty heads can be `live` with no git objects. 须人工核 how often exported task proofs lack heads; the reviewed-task export path does pin heads and fail-closes without `--git-dir`.

Host compile never reads `tools.json`. PATH hits stay `discovered_unintegrated` (`probe.py` 19–36) and cannot `run_task` (`test_capability.py` 143–167). Default write is `config.root / .dyro/host-projections/`; `--user` writes `registry_home()/host-projections/<workspace>` (`compile.py` 67–70). Never-compiled does not block apply (`doctor.py` 70–74; `test_host.py` 287–297). Stale compiled raises before lease/Task APIs (`supervision.py` 390–392; `test_host.py` 299–310). Message says “已降为 plan-only”; the wave is aborted, not rewritten to plan-only actions. No `ActivationLease` type exists in this tree. 须人工核 any out-of-tree automatic mutation tick.

## Decision Validity

Locked decisions that source confirms: A1; dirty task workspace still rejected via `_collect_task_heads` inside local `_valid_review_acceptance`; P4 target types; Authority B compile-always (OpenCode fixture without hook is `skill_only`, `test_host.py` 140–161); fake/absolute `hook_surface` does not write a hook (163–197); hook text is intent-lattice JSON, not a sandbox (`compile.py` 300–314).

Open decisions (position only):

1. **Deny hook only under `host-projections/` is a false-authority gap, not an A1/B1 break.** `capability test` only proves the Card path exists (`probe.py` 64–76). Compile writes `.dyro/host-projections/<host>/deny-hook.json` (`compile.py` 335–340), never the Card’s `hook_surface`. Doctor then attests `skill_and_hook` against *that* artifact (`doctor.py` 154–162). No in-tree host loader consumes `deny-hook.json`. 须人工核 whether any external host maps that file. Until then `authority_projection=skill_and_hook` is an artifact flag. Locked “hook is not OS isolation” stops this from being P0.

2. **Yes: shipping this tree as 0.6.0 is a version lie.** `pyproject.toml` is still `0.6.0`. README identity sentences, `schema_version = 1` export, and non-experimental `verify-bundle` have landed (`test_proof_cli.py` 116–117). Published 0.6.0 did not have those commands or that identity. `tools/verify_release_gates.py` 50–52 **skips** the 1.0 file gates while version ≠ `1.0.0`, so this tree can be tagged/published as 0.6.x with 1.0 contracts and a green “skip”. Treating 1.0 as releasable requires `version = "1.0.0"`. Publishing this tree as 0.6.0 is also forbidden by the plan’s “不回写 0.6.0 已发布语义”.

3. **Repeatable `--git-dir` is enough as a mechanism; 1.0 docs must say the caller assembles objects.** `action=append` plus `any(cat-file)` (`bundle.py` 263–264) does not map `repo_id` → object store and does not read workspace layout. Missing objects → `inconclusive`. That is correct B1 fail-closed. Docs/help must not imply Dyro will gather polyrepo objects. `_SHA_RE` allows 7-char abbreviations (`bundle.py` 22); short SHAs plus `any()` across dirs can resolve in the wrong store. Require full hex (40 or 64) before calling this 1.0-portable.

## Plan Executability

P1–P5, P8–P12a, and the P13 identity strings are present and tested at the happy path. P7 Console Proof is correctly parked (`not_inspected`). P4 production `BudgetUsage` is not wired (`live_merge_evidence` only used in `test_proof_decay.py`).

Missing tests that leave the P0 unguarded: no case deletes `manifest.proof_sha256` (or one id) and asserts `inconclusive`, not `live`. `test_proof_cli.py` 124 only checks the key exists on a honest export.

P13 “sdist 干净环境陌生人核验” is not what runs. `tools/verify_bundle_stranger.py` builds a hand-made zip and invokes whatever argv the test passes (`tests/test_proof_bundle.py` 76–80: `sys.executable -m dyro`). That is in-tree CLI, not an sdist install. `verify_release_gates.py` is substring presence, not behavior.

`agent add` still writes `[adapters.*]` (`cli.py` 840–851), not `[[capabilities]]`. Load-time upgrade still yields Cards. 0.8 P9 wording is unmet; 1.0 behavior is compatible. Not a merge-truth break.

须人工核: bitwise accept/reject vs the published 0.6.0 sdist. This tree’s `merge_task` / `check_dispatchable` control flow matches the locked A1 shape; a binary/sdist diff was not in the audit set.

## Scope And Risk

Scope stayed on the projection train: Proof, Cards, host skill, portable verify. Merge/downstream predicates were not replaced. Risk that blocks 1.0 is the verify-bundle digest skip (stranger can rewrite pins) and the 0.6.0 identity (users and release gates cannot tell which contract they installed). Residual risk: `skill_and_hook` overclaim; short SHA / unmapped polyrepo `--git-dir`; missing-file Proofs labeled `decayed` instead of `inconclusive`. Those do not change 0.6 merge/downstream truth.

## Go/No-Go

**Conditional Go** for treating 1.0 as releasable.

Not Go: B1 integrity is bypassable without `proof_sha256`, and the package still claims 0.6.0.  
Not No-Go: A1 is intact; dirty workspace still rejected; never-compiled host projections do not block apply; PATH/`tools.json` cannot execute; workspace `verify` and `verify-bundle` are separate conclusions on the tested export path.

Do not tag `v1.0.0` or publish this tree as 0.6.0 until Required Fixes P0 are done.

## Required Fixes

**P0 — `verify-bundle` must fail closed when a member digest is missing**  
File: `src/dyro/proof/bundle.py` 99–102, 224–227.  
B1: integrity of the Proof Bundle. `if expected and …` treats omitted/empty `proof_sha256` as success, then `_integrity_of` can return `live`.  
Fix: `_valid_manifest` must require `proof_ids` to be a non-empty string list and `proof_sha256` to be a dict with a non-empty hex digest for every id. If a digest is missing or mismatched → `BUNDLE_BYTES_MISMATCH` / `inconclusive`, never `live`. Add a test that rewrites `proofs/<id>.json` after deleting that digest and asserts not `live`.

**P0 — do not release this tree as 0.6.0; do not call it 1.0 until the version is 1.0.0**  
Files: `pyproject.toml` 7; `tools/verify_release_gates.py` 50–52.  
1.0 identity + `verify-bundle` already landed under `version = "0.6.0"`, and the 1.0 gate no-ops until the number changes.  
Fix: set `project.version` to `1.0.0` only after the P0 digest fix; refuse any 0.6.x publish of this tree. Keep the skip only for unrelated 0.6.x maintenance lines that do not contain `src/dyro/proof/bundle.py`.

**P1 — missing / unparseable evidence must stay `inconclusive` after evaluate**  
Files: `src/dyro/proof/derive.py` 374; `src/dyro/proof/evaluate.py` 49–65, 98–102.  
ADR/plan: 缺文件、缺工具、不可解析 → `inconclusive`. `_valid_review_acceptance` / `_valid_external_signoff` return `False` for those cases, so `list`/`verify` report `decayed`.  
Fix: either make `_predicate` distinguish missing/unparseable (`None`) from failed rebind (`False`), or honor `force_inconclusive` in `evaluate_proof` and skip the predicate. Extend `test_missing_review_binding_is_inconclusive` through `evaluate_proofs` / `proof verify`.

**P1 — `skill_and_hook` must not imply the host loaded the deny hook**  
Files: `src/dyro/host/compile.py` 194–209, 335–340; `src/dyro/capability/probe.py` 64–76.  
Deny hook is written only under `host-projections/`, not `hook_surface`.  
Fix (pick one, do not open a new product line): document in compile/doctor JSON that `skill_and_hook` means “artifact written beside SKILL.md, not installed at hook_surface”; or stop emitting `skill_and_hook` until a host-specific install path exists. Do not write into `hook_surface` unless a later decision explicitly expands compile authority.

**P1 — portable git pins must be full object IDs; docs must say the caller assembles stores**  
Files: `src/dyro/proof/bundle.py` 22, 177–184, 263–268; `src/dyro/cli.py` 2838–2842.  
`--git-dir` repeat is the right mechanism. 7-char `any()` lookup is not polyrepo-safe. Help text “缺省不得报 live” is false for empty `repo_heads`.  
Fix: accept only 40- or 64-char hex; keep missing objects `inconclusive`; state in verify-bundle help that the caller must pass every object store that contains the pinned SHAs. Align help with the empty-heads case (inconclusive if the bundle declared heads; do not claim a blanket “no git-dir ⇒ never live” unless you also require `--git-dir` whenever the zip exists).

**P2 — P13 stranger job is not an sdist install**  
Files: `tools/verify_bundle_stranger.py`; `tests/test_proof_bundle.py` 76–80.  
Plan P13 asked for a clean sdist environment. Current job is in-tree `python -m dyro`.  
Fix before a 1.0 tag: install the sdist into a throwaway env, then run the same fixture. Not required to restore A1.


# Security Reviewer Review Section

Reviewer: security-reviewer  
Time: 2026-08-15  
Verdict: **No-Go**

## Contract Consistency

B1 / ADR-0006 / P6 and the CLI help agree: `verify-bundle` is integrity, not identity; missing procedure / substrate / git objects / required declared keys must be `inconclusive`; no `--git-dir` must not be `live`; evidence ZIPs must not be `live`; hook is not a sandbox; PATH discovery is not execute.

Source matches the **labels** (`mode=integrity`, help text「不是身份证明」, skill negatives, `discovered_unintegrated` never compiled into execute). Source **breaks the live/fail-closed predicates**.

| Contract | Source |
|---|---|
| P6 / CLI `--git-dir`「缺省不得报 live」 | `_integrity_of` returns `LIVE` when `repo_heads` SHAs are empty, even with `git_dirs=()` (`bundle.py:178-184`) |
| Bundle byte integrity | `proof_sha256` is optional; empty/missing skips the hash (`bundle.py:99-102`, `_valid_manifest` only checks `kind` + `schema_version` at `224-227`) |
| 缺已声明的签名密钥 → inconclusive | Only `value == "true"` (`bundle.py:220-221`); JSON `true` becomes `"True"` via `str(value)` (`bundle.py:151`) and does **not** require keys |
| Evidence ZIP → inconclusive | Holds for real `task evidence` layout (`bundle.py:80-81`, `230-232`; `test_proof_cli.py:194-206`). Adding `manifest.json` disables the evidence detector; that is forging, not accidental accept |
| Authority B: hook is not a sandbox | CLI/help and hook JSON omit “sandbox”. Hook is written only under `host-projections/`, never to `hook_surface` (`compile.py:338-340`) |
| PATH ≠ executor | `discover_unintegrated` is list-only (`probe.py:19-36`); `available` requires `execute` + adapter probe (`compile.py:187-193`) |
| Never-compiled compatibility | Intentional: `assert_projections_allow_mutation` returns if no `*.toml` (`doctor.py:70-74`; `test_host.py:287-297`) |
| A1 merge/dispatch unread Proof store | No `merge_task` / `check_dispatchable` read of the bundle path in this tree (out of this mutation-gate slice; not reopened) |

`process.run` is argv, no shell (`process.py:18-50`). `git --git-dir=` / `git -C` / `cat-file -e` / `merge-base` get a path or a SHA matching `^[0-9a-f]{7,64}$`. No command-injection gadget in this slice. ZIP members are read, not extracted; `_read_member` rejects absolute / `..` (`bundle.py:240-250`). Classic zip-slip write is not present.

## Source Evidence Accuracy

Proven from this tree only (not inherited):

1. **False `live` without caller git objects (P0).**  
   `_has_substrate` is true for `bytes_sha256` / `plan_sha256` / `attempt_id` / `contract_hash` with **no** heads (`bundle.py:210-217`). Then `shas=[]` skips `MISSING_GIT` and, with `current_heads is None`, returns `LIVE` + `STILL_BOUND` (`bundle.py:178-184`).  
   Honest path: gate/review export when `task-heads.json` is absent (`derive.py:464-467`, `132-187`, `191+`). Stranger CI/fixture always pins a head (`verify_bundle_stranger.py:50-51`), so it does not catch this.  
   CLI still says「缺省不得报 live」(`cli.py:2838-2842`).

2. **Integrity hash is advisory (P0).**  
   `if expected and sha256(raw) != expected` (`bundle.py:99-102`). A stranger-supplied ZIP with valid `kind`/`schema_version` and omitted `proof_sha256` is parsed and can exit 0 (`verify_exit_code` all-`live` → 0, `project.py:73-79`).

3. **Require-signed + missing keys can still be `live` (P0).**  
   Honest export writes `"true"`/`"false"` strings (`derive.py:413-417`). Verify stringifies arbitrary JSON (`bundle.py:151`). `{"require_signed_review": true}` + `declared_key_ids: []` does not hit `MISSING_DECLARED_KEYS`. Declared key **IDs** are never bound to a keyring (correct for B1-not-identity; must stay labeled).

4. **Mutation gate is fail-open on “no manifests”.**  
   `compiled = bool(manifests)` and manifests are only `*.toml` (`doctor.py:58, 114-123`). Only caller is `apply_supervised_wave` (`supervision.py:390-392`). `task run` / `task merge` are ungated. `ActivationLease` **does not exist** under `src/` — P12 “若存在 ActivationLease” is N/A in this tree.  
   Deleting `*.toml` while leaving `SKILL.md` / `deny-hook.json` reports 未编译 and apply proceeds. Whether any host actually loads `.dyro/host-projections/` is **须人工核**; the Dyro apply predicate is source-proven.

5. **Host skill contract is mostly held, with one injection surface.**  
   Available rows require `Intent.EXECUTE` and `test_capability.executable` (`compile.py:187-193`). Observe-only cards are omitted. `_FORBIDDEN_SKILL_MARKERS` + “不要执行” (`compile.py:317-332`) hold for compiler-owned text. `cannot_prove` is interpolated raw into the markdown table (`compile.py:266-267`) and is **not** `validate_id`-constrained (`cards.py:69, 129-134`). A card can smuggle `dyro objective apply` / extra fences that are not in the denylist. Needs write access to `dyro.toml` (already a trusted plane).

6. **Secrets in export/skill/hook.**  
   `proof_payload` has ids, hashes, procedure strings, `declared_key_ids`, policy snapshot (`project.py:15-39`). Gate argv/cwd are hashed, not exported (`derive.py:138, 482-484`). `[[capabilities]]` rejects `env` (`cards.py:52-53`). Adapter has no `env` field (`config.py:42-46, 245-253`). Compile JSON emits hashes/relpaths, not skill body (`cli.py:950-961`). Hook JSON is deny lattice only (`compile.py:300-314`). No hardcoded credentials in the reviewed files. `capability test` prints probed `executable` paths (`cli.py:931-945`) — local path disclosure, not bundle leak.

7. **Dependencies.**  
   OSV query on locked runtime: `cryptography==49.0.0` → GHSA-g6cj-pr64-35w5 / CVE-2026-69247 (PKCS#7 EnvelopedData oracle, fixed in 50.0.0). Dyro `signing.py` uses Ed25519 only, not `pkcs7_decrypt_*`. Reachability through this feature set is **须人工核 / likely unused**. `rfc8785==0.1.4`, `cffi==2.1.0`, `pycparser==3.0`: 0 vulns. `pip-audit` could not run in this environment (venv `ensurepip` abort).

## Decision Validity

**Open 1 — hook only under `host-projections/`, not `hook_surface`.**  
Valid under Authority B. Writing the deny file into the host’s real hook pipeline would *expand* mutation surface. Security implication: `authority_projection=skill_and_hook` is a **compiled artifact label**, not an armed intercept. Doctor `FRESH` + `skill_and_hook` does not mean integrate/publish are denied at the host. CLI already says 不是沙箱 / 不是隔离. Do not reopen B; do not advertise the hook as enforcement.

**Open 2 — version still `0.6.0` with 1.0-shaped commands.**  
`pyproject.toml:7` is `0.6.0`. `verify-bundle` + frozen `schema_version=1` + unlabeled export (`test_proof_cli.py:116` asserts export is **not** “experimental”) are 1.0/P13 shapes. `verify_release_gates.py:50-51` **skips** 1.0 gates on non-1.0.0. Trust implication: a 0.6.0 wheel already exposes the portable-integrity API, and that API can return `live` incorrectly (P0 above). This is a labeling/trust defect, not a reason to reopen the version number by itself.

**Open 3 — caller `--git-dir` / object-existence vs identity.**  
Do **not** reopen B1. Object existence (`cat-file -e` in **any** provided git-dir, `bundle.py:263-273`) is integrity, not “this SHA is the named repo’s commit on the sender’s machine.” Repo keys are not bound to a specific `--git-dir`. That hole must stay labeled (`mode=integrity`). Additional integrity weakness (not identity): `_SHA_RE` allows 7-char abbreviations (`bundle.py:22, 181`). Keep B1; tighten abbreviation if 1.0 “钉死 SHA 可解析” means a specific object.

## Plan Executability

| Plan gate | Executable as written? |
|---|---|
| P6「不提供 git 对象时为 inconclusive」 | **No** — empty-heads / hashless-substrate proofs are `live` |
| P6 evidence ZIP | **Yes** for stock evidence layout |
| P10 PATH discovery | **Yes** — not an executor; `capability add` is explicit |
| P11 skill-only `dyro next` | **Mostly** — broken by `cannot_prove` interpolation |
| P12 stale/tamper blocks apply | **Yes** when `*.toml` remains; **escape hatch** when tomls are removed |
| P12a hook optional | **Yes** — missing/fake/absolute `hook_surface` → `skill_only` (`compile.py:64-76`, tests in `test_host.py`) |
| P13 stranger CI | **Partial** — `ci.yml` wheel-smoke runs `verify_bundle_stranger.py`; `pypi-publish.yml` smoke does **not** |
| P13 1.0 tag refuse | String-presence gates only (`verify_release_gates.py`); does not execute stranger verify or the P0 predicates |

The 1.0 “stranger + caller git objects ⇒ same integrity conclusion” story is implementable, but current `verify_bundle` acceptance is wider than the story.

## Scope And Risk

**Overall risk: HIGH** (1.0 portable-integrity claim is not fail-closed).

- **Blast radius of P0 `live`:** remote/untrusted ZIP + local CLI/`CI` consumer that keys off `status==live` or exit 0. No git objects required. No manifest byte hash required. Can look like a successful stranger verify. Does not grant merge (A1 still holds) — it **lies about integrity**.
- **Blast radius of mutation gate:** local workspace operator (or anything that can unlink `.dyro/host-projections/*.toml`). Blocks only `objective apply`, not `task run`/`merge`. Never-compiled remains a fixed compatibility choice.
- **Command injection / zip-slip write / PATH-as-executor / adapter-env-in-bundle:** not found in this slice.
- **`--git-dir` identity:** labeled; caller can point at any readable object store they already control. Local tool, not a remote identity assertion.

## Go/No-Go

**No-Go for 1.0 releasability.**  
`verify-bundle` is the 1.0 hard gate and can report `live` without caller git objects, without per-proof `proof_sha256`, and without declared keys when require-signed is a JSON boolean. That is integrity theater. Host/capability planes are closer to contract (PATH, observe-only, hook-not-sandbox) and are not the release blocker.

Not Conditional Go: the 1.0 product sentence is already false in source. After the P0s below, this becomes Conditional Go (P1s can ride a 1.0.0 RC).

## Required Fixes

### P0 — `verify-bundle` must not be `live` without caller git objects

`bundle.py` `_integrity_of` / CLI help / P6 acceptance.

```python
# BAD (current): empty shas ⇒ skip git, then LIVE
shas = [sha for _repo, sha in proof.substrate.repo_heads if sha]
if shas and not git_dirs:
    return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=MISSING_GIT)

# GOOD: no caller object store ⇒ never live (matches cli.py help)
if not git_dirs:
    return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=MISSING_GIT)
if not shas:
    return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=MISSING_GIT)
```

Add a unit test: procedure + `bytes_sha256`/`plan_sha256`, **no** `repo_heads`, `git_dirs=()` → `inconclusive` / `missing_git`, exit 3. The current stranger fixture must stay `live` only **with** `--git-dir`.

### P0 — require `proof_sha256` for every `proof_id`

```python
# BAD
expected = (manifest.get("proof_sha256") or {}).get(proof_id)
if expected and hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected:
    ...

# GOOD
digests = manifest.get("proof_sha256")
if not isinstance(digests, dict) or proof_id not in digests:
    proofs.append(_bundle_inconclusive(BUNDLE_BYTES_MISMATCH, f"缺少 {proof_id} 哈希"))
    continue
if hashlib.sha256(raw.encode("utf-8")).hexdigest() != digests[proof_id]:
    proofs.append(_bundle_inconclusive(BUNDLE_BYTES_MISMATCH, f"{proof_id} 字节哈希漂移"))
    continue
```

`_valid_manifest` must require `proof_ids: list[str]` and a digest for each id.

### P0 — require-signed must fail-closed on missing keys

```python
# BAD
return any(value == "true" for _key, value in proof.policy_require_signed)

# GOOD
def _flag_on(value: str) -> bool:
    return value in {"true", "True", "1", "yes"}  # or reject non-{true,false} at parse

# and in proof_from_payload: only accept bool or "true"/"false";
# bool True must become "true", not str(True)=="True"
```

Test: `policy_require_signed: {"require_signed_review": true}` + empty `declared_key_ids` → `missing_declared_keys`, never `live`.

### P1 — mutation-gate escape hatch (do not reopen never-compiled)

If any `SKILL.md` / `deny-hook.json` exists under `host-projections/` without a valid matching `*.toml`, treat as `compiled=True` and `INVALID`/`TAMPERED`, not 未编译. Keep “zero projection files ⇒ allow apply”.

### P1 — host skill markdown injection

Escape or reject `cannot_prove` / table fields that contain newlines, backticks, or `_FORBIDDEN_SKILL_MARKERS`. Compiler must not be bypassable by a Card string.

### P1 — integrity hardening that stays inside B1

- Require full `^[0-9a-f]{40}$` or `{64}$` for pins and `--current-heads` (drop 7-char `cat-file` abbreviations).  
- Keep “object exists in caller git-dir ≠ identity” labeled; do not bind this to merge `live`.  
- Run `tools/verify_bundle_stranger.py` on the **publish** job, not only `ci.yml` wheel-smoke.  
- Do not ship 1.0-shaped `verify-bundle` as if it were already the 1.0 guarantee while version is `0.6.0` and P0s are open (docs/help: experimental / not 1.0-hard, or bump only after P0s).

### P2

- Zip member size cap (local zip-bomb).  
- `authority_projection=skill_and_hook` copy: “compiled deny file; not installed on hook_surface; not a sandbox.”  
- Bump `cryptography` to `>=50.0.0` (CVE-2026-69247); unused PKCS#7 path in this slice.  
- `inspect_projections` empty-findings `ok=True` when `compiled=False` is fine; do not let that JSON be read as “projections healthy.”

**须人工核:** whether any supported host auto-loads `.dyro/host-projections/**/SKILL.md`; whether any 1.0 consumer ignores `mode` and trusts `status==live`; whether `objective apply` is the only 1.0 “automatic mutation” path they will claim.


# Critic Review Section

Reviewer: critic  
Time: 2026-08-15  
Verdict: **No-Go**

Mode: ADVERSARIAL (3+ MAJOR after first pass; adjacent host/publish/derive paths were then hunted). A1/B1 as *design locks* are not reopened. Source was checked for *implementation* violations only.

---

## Contract Consistency

**A1 is kept.** `merge_task` / `check_dispatchable` still call `_valid_review_acceptance` / `_valid_external_signoff` / `_assert_dependency_integrated` / `_prepare_merge`. `tasks.py` has no Proof import. `build_task_readiness` does not read `decayed_merge_subjects`. Planner emits `PROOF_DECAYED` as attention only. Console summary forces `inspect_integration=False` → `inspect_proofs=False` and hard-codes `proof_inspection=not_inspected`. `live_merge_evidence` is not wired into production `BudgetUsage`. Line dirty stays `_prepare_merge` text, not `PROOF_DECAYED`.

**B1’s two-command split is kept.** `proof verify` prints `mode=rebind`. `verify-bundle` prints `mode=integrity`. Tests prove workspace `decayed` + bare bundle `live` on the same ZIP. `--current-heads` is the only decay path. Bundle ZIP has no git object DB.

**B1’s fail-closed clause is not kept.** ADR-0006 decision 10 and the 1.0 SSOT say missing git objects ⇒ `inconclusive`, never `live`. Implementation only does that when pinned SHAs exist:

```177:184:dyroengineeringflow/src/dyro/proof/bundle.py
    shas = [sha for _repo, sha in proof.substrate.repo_heads if sha]
    if shas and not git_dirs:
        return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=MISSING_GIT)
    ...
    if current_heads is None:
        return replace(proof, status=ProofStatus.LIVE, decay_reason=STILL_BOUND)
```

CLI help states the opposite: `"缺省不得报 live"` (`cli.py:2842`). `_has_substrate` is true on `bytes_sha256` / `plan_sha256` / `attempt_id` alone (`bundle.py:210-217`). Derive will export a `review_verdict` with empty `repo_heads` when `task-heads.json` is missing (`derive.py:464-467`, `245-254`). That bundle is `live` with no `--git-dir`.

**Authority B is not reopened, but `skill_and_hook` is a label lie.** Proven `hook_surface` is only an existence probe. Deny hook is written to `.dyro/host-projections/<host>/deny-hook.json`, never copied onto the declared surface (`compile.py:194-223`, `335-342`; `test_host.py:199-218` creates `hooks/surface` then asserts the hook under `projection_root`). Adapter upgrades never set `hook_surface` (`cards.py:17-32`). `card_payload` still echoes the *declared* path (`probe.py:91`), not the proven one.

---

## Source Evidence Accuracy

| Claim | Source | Accurate? |
| --- | --- | --- |
| `merge_task` / `check_dispatchable` do not read Proof store | `tasks.py:545-556`, `2058-2068`; no `proof` import | Yes |
| `PROOF_DECAYED` is extra copy, not a new reject set | `tasks.py:2062-2067`; `test_proof_decay.py:281-366` | Yes for accept/reject. Copy is sloppy: *any* failed review/signoff is labeled `PROOF_DECAYED`, including never-valid review |
| Two commands, two conclusions | `cli.py:1911-1954`, `2833-2848`; `project.py:42-50`; `test_proof_cli.py:208-229` | Yes |
| `schema_version = 1` | `bundle.py:19, 47-52` | Yes |
| Stranger + caller git ⇒ integrity `live`; no git ⇒ not `live` | CI `ci.yml:91`; `verify_bundle_stranger.py`; `test_proof_cli.py:141-145` | Yes **only** for SHA-bearing bundles |
| Identity sentence in every README language | `test_readme_identity.py:9-27`; `README.md:7` | Phrase present. Lede still says “delivery control platform” / “agent launchers” (`README.md:5`) |
| `verify-bundle` is a 1.0 hard gate | `verify_release_gates.py:50-55`; `pypi-publish.yml:73-76` | **No.** Gate **skips** unless version/tag is already `1.0.0`. “Evidence” is substring presence. P5 marker is `_assert_dependency_integrated` in `graph.py` — 0.6 code, not A1 |
| Missing P5/P6-export/P12 refuses `1.0.0` | same | **Theater.** Deleting A1 (wire `merge_task` to Proof) still passes if the strings remain |
| P13 stranger-from-sdist on **publish** artifacts | `pypi-publish.yml:81-97` vs `ci.yml:91` | **No.** Publish sdist smoke does dispatch doctor / assets only. Stranger script is CI-only |
| Package is 0.7 / 0.8 / 0.9 / 1.0 | `pyproject.toml:7` `version = "0.6.0"` | **No** |
| README documents caller git objects | `README.md` — zero hits for `proof`, `verify-bundle`, `git-dir` | **No** |
| Export is 0.7 experimental | `cli.py:2828`; `test_proof_cli.py:116` `assertNotIn("experimental")` | Code treats it as frozen 1.0 schema on a 0.6.0 package |

`store.py` from the plan module map was never created. That is fine: list/verify re-derive (`derive.py:34-53`). Do not treat the missing file as a lock break.

---

## Decision Validity

**A1/B1/authority B/product category:** do not reopen. Implementation kept A1 and the B1 *split*. It missed B1 *fail-closed* for SHA-less proofs.

**Open 1 — hook not copied onto `hook_surface`:** product-dishonest. `authority_projection=skill_and_hook` means “a JSON sidecar exists in Dyro’s projection dir because a relative path existed.” The host intercept directory is untouched. CLI correctly refuses to call this a sandbox (`cli.py:2487-2512`). The authority enum still overclaims.

**Open 2 — 0.6.0 vs 1.0 capability:** ship lie either direction. This tree implements 0.7–1.0 commands, freezes `schema_version=1`, and prints the 1.0 identity sentence, while `pyproject.toml` is `0.6.0`. ADR-0006: `v0.6.x` is maintenance-only and must not absorb Host Compiler. Publishing this as `0.6.0` is a lie. Tagging `1.0.0` without bumping version is impossible (`pypi-publish.yml:67-70`) and would still be a lie because the 1.0 brake is fake and README cannot teach the stranger contract.

**Open 3 — caller must bring git objects:** documented at CLI help, **not** at README. 1.0 SSOT required identity sentences (done) *and* a stranger who can reproduce integrity. A stranger who only reads README never learns `--git-dir`. That is not 1.0 product surface.

---

## Plan Executability

P1–P12a code exists and A1 workspace tests are real (`test_proof_decay.py`). P13 as written is **not** what landed:

- Plan P13: “从 sdist 安装的干净环境…跑 `verify-bundle`” on **release artifacts**. Landed in CI only.
- Plan P13: “缺 P5/P6-export/P12 任一证据，发布工作流拒绝打 `1.0.0`”. Landed as `marker in file.read_text()`.
- Plan P6 ZIP list includes “被核验字节”. Export writes only proof JSON + `bytes_sha256` (`bundle.py:35-60`). Source wins; this matches locked B1 (hash + resolvable SHA), not the ZIP shopping list. Not scored as A1/B1 break.
- `_build` is a tautology: `INCONCLUSIVE if force_inconclusive else INCONCLUSIVE` (`derive.py:374`). Harmless because evaluate overwrites status. Dead parameter.

An executor following only the plan’s 1.0 exit checklist and then reading `verify_release_gates.py` would believe the tag brake exists. It does not encode the exits.

---

## Scope And Risk

Blast radius if this is called 1.0-releasable:

1. **False 1.0 tag.** Bump `pyproject` to `1.0.0`, keep the magic strings, break A1 in `merge_task`, update the few decay tests, gate still prints `1.0 gates present`. No AST lock that `tasks.py` must not import `dyro.proof`.
2. **False integrity `live`.** SHA-less / heads-missing export + no `--git-dir` ⇒ exit 0, `status=live`, `mode=integrity`. Help text says that cannot happen.
3. **False hook authority.** `skill_and_hook` while the proven surface has no deny file. Host `git merge` is unchanged. Design already says Core is the real gate — the enum still sells a second seatbelt that was never buckled.
4. **Version confusion.** PyPI `0.6.0` with `verify-bundle` / `host compile` / `capability` / 1.0 identity. Or a `1.0.0` tag whose “hard gate” never ran stranger-from-sdist on the publish job.

Merge-truth for current `task merge` / downstream ready set is **not** the lie. The 1.0 *tag* is.

---

## Go/No-Go

**No-Go for calling this 1.0-releasable.**

A1 merge-truth: Go (do not reopen).  
B1 two-command split: Go.  
B1 missing-git fail-closed + 1.0 packaging/docs/gates: No-Go.

Claiming 1.0-readiness today is a lie: version is `0.6.0`, the 1.0 brake is a string scan that skips on this version, publish does not run the stranger job, README cannot state the caller-git contract, and `verify-bundle` can still print `live` without `--git-dir`.

---

## Required Fixes

### P0 — would make a 1.0 tag or merge-truth claim false

1. **Do not tag `1.0.0` and do not publish this tree as `0.6.0`.** `pyproject.toml:7` is `0.6.0`. README already ships the 1.0 identity sentence. CLI freezes `schema_version=1` and asserts export is not experimental (`test_proof_cli.py:116`). Either number is a ship lie until version, gates, and README match one train car.
   - Fix: pick a version, bump `pyproject.toml`, and make the publish job fail closed on that number’s real exits. `0.6.x` must not contain Proof/Card/Compiler.

2. **`verify-bundle` without caller git objects must not be `live`.** `_integrity_of` (`bundle.py:177-184`) + CLI help (`cli.py:2842`) + ADR-0006 decision 10 + 1.0 SSOT are inconsistent. Empty `repo_heads` + any `bytes_sha256`/`plan_sha256` ⇒ `live` with `git_dirs=()`.
   - Fix: if `git_dirs` is empty, return `inconclusive`/`missing_git` unconditionally. Add a test that exports a heads-missing `review_verdict` and asserts no `--git-dir` ⇒ not `live`. Keep the existing SHA-bearing stranger test.

3. **1.0 refuse-tag gate is not a gate.** `verify_release_gates.py:10-22,50-55` skips on `0.6.0` and accepts `def verify_bundle` / `_assert_dependency_integrated` as “P5 evidence”. `pypi-publish.yml:81-97` never runs `tools/verify_bundle_stranger.py` against the sdist `dyro`.
   - Fix: on tag `v1.0.0` / version `1.0.0`, fail unless (a) sdist-installed `dyro proof verify-bundle` on the fixture is `mode=integrity` and not `decayed`, (b) same command without `--git-dir` is not `live`, (c) `tasks.py` AST/import lock: `merge_task` / `check_dispatchable` do not reference `dyro.proof`. Delete the `graph.py` substring as P5 evidence.

### P1

4. **README (all languages) must state the two-command contract.** Identity phrase is not enough. A 1.0 stranger who only reads `README.md` never sees `--git-dir`, integrity ≠ merge, or `inconclusive` on missing objects. CLI help is not the 1.0 surface.
   - Fix: one short subsection in every `README*.md`, same meaning, covered by `test_readme_identity.py` (not just the physics noun).

5. **`skill_and_hook` must not be claimed unless the deny hook is installed on the proven surface, or the enum/docs must say `projection_sidecar`.** `compile.py:208-222` writes `.dyro/host-projections/.../deny-hook.json`. `hooks/surface` stays empty. `card_from_adapter` never copies a hook path (`cards.py:17-32`).
   - Fix (pick one): copy/link the rendered hook onto the proven relative surface, **or** never set `AUTHORITY_SKILL_AND_HOOK` unless that copy succeeds, **or** rename the status and document that hook files are Dyro-side only. `capability list` JSON must not show an unproven declared `hook_surface` as if tested (`probe.py:79-91` vs `64-76`).

6. **Lock A1 in tests the way dispatch locks merge.** `test_dispatch_boundary.py` already AST-bans `merge_task` in dispatch. There is no equivalent that `tasks.py` cannot import `dyro.proof`. Without it, a later PR can make Proof a second merge gate and still pass `verify_release_gates`.
   - Fix: AST/import test on `merge_task` / `check_dispatchable` / `_prepare_merge`.

7. **Manifest integrity must require `proof_sha256`.** `bundle.py:99-101` skips the byte check when the field is missing; `_valid_manifest` (`224-227`) only checks `kind` + `schema_version`.
   - Fix: missing/empty `proof_sha256` ⇒ `inconclusive`, not `live`.

### P2

8. JSON `status=live` plus `mode=integrity` is enough for a careful reader; it is not enough for a script that only checks `status`. Add `conclusion: integrity` / `merge_equivalent: false` if you want automation-safe B1. Not required to keep the lock.
9. `merge_task` stamps `PROOF_DECAYED` on every failed review/signoff (`tasks.py:2063-2067`), including never-bound review. Design: only live→decayed merge copy. Change the message or only attach the code when a derived review Proof is actually `decayed`.
10. README lede still sells “delivery control platform” and “agent launchers” (`README.md:5`) beside the physics identity (`README.md:7`). Identity test only `assertIn` the physics noun.
11. `derive.py:374` tautology — delete `force_inconclusive` or make it real.
12. Evidence ZIP detector (`bundle.py:230-232`) misses `receipt.md` + `manifest.json` together. Fail closed if evidence markers appear anywhere.

---

**Pre-commitment vs found:** expected A1 leak (absent), B1 command collapse (absent), version lie (present), CLI/JSON merge confusion (help is good; README silent; help overclaims), missing 1.0 tests (present), hook-surface honesty (present). A1 is the part that is actually solid. The 1.0 sticker is not.


# Architect Review Section

Reviewer: architect  
Time: 2026-08-15  
Verdict: Conditional Go

Locked A1 / B1 / authority B still hold on the merge, ready-set, and host-mutation paths. The implementation is freezeable after bounded contract fixes. It is not a 1.0 freeze today because portable verify and hook-authority labels can still emit the wrong 3-state, and export still carries workspace rebind status inside the stranger artifact.

## Contract Consistency

Producer → evaluate → CLI verify is wired, but the 3-state is not preserved across the seam.

- `list_proofs()` always re-derives, then calls `evaluate_proofs()` (`derive.py:26-53`). There is no Proof store and no merge-truth cache. That matches locked micro-decision 5.
- `derive._build()` forces `ProofStatus.INCONCLUSIVE` on both branches (`derive.py:374`). Derive never forges `live`. P1 tests assert that (`tests/test_proof_derive.py:125-136`).
- `evaluate_proof()` then discards derive status and maps 0.6 booleans: `False` → `decayed`, exception → `inconclusive` (`evaluate.py:49-66`, `evaluate.py:98-102`). Missing / unbound `review.md` therefore becomes `decayed` on `dyro proof verify`, even though Appendix A and `test_missing_review_binding_is_inconclusive` require `inconclusive` at derive time. CLI verify and derive no longer share a 3-state contract.
- `merge_task()` / `check_dispatchable()` do not import or read Proof (`tasks.py:545-556`, `tasks.py:2058-2068`). `build_task_readiness()` never consults `decayed_merge_subjects` (`planner.py:70-176`). `PROOF_DECAYED` is attention-only (`planner.py:272-281`). A1 ready-set contract holds.
- `task explain` reuses `_assert_dependency_integrated()` (`graph.py:195-198`), not Proof cache. Matches P5.
- Export → stranger verify is a different function, but the ZIP is built from already-evaluated workspace Proofs (`cli.py:1926-1937` → `export_bundle()` → `proof_payload()` at `bundle.py:43-46` / `project.py:15-38`). The portable bytes contain workspace `status` / `decay_reason` / `observed_at`. `verify_bundle()` ignores those fields and recomputes integrity (`bundle.py:149`, `bundle.py:165-185`), and CLI prints `mode=integrity` (`cli.py:1940-1951`). Conclusions are computationally split; the artifact still mixes them.
- `verify-bundle` without `--current-heads` cannot emit `decayed` (`bundle.py:183-184`). Evidence ZIP → `inconclusive` (`bundle.py:80-81`, `bundle.py:230-232`). Heads-present + no `--git-dir` → `missing_git` (`bundle.py:177-179`). That is the B1 happy path.
- B1 hole: git is required only when `repo_heads` contain SHAs. Headless proofs (`action_receipt`, `gate_log` with empty heads) can be `live` with `git_dirs=()`. CLI help says the opposite (`cli.py:2838-2842`). Architecture invariant 18 is absolute; this implementation is SHA-conditional.
- `_requires_declared_keys()` is `any(policy == "true")` (`bundle.py:220-221`). `_policy_snapshot()` stamps both `require_signed_review` and `require_signed_signoff` onto every kind (`derive.py:413-418`). A signed-review workspace therefore makes `gate_log` / `integration_heads` `inconclusive` for missing keys they never declare.
- Multi-repo assembly is a bag of `--git-dir` values. `_object_exists()` / `_is_ancestor()` succeed if any dir resolves the SHA (`bundle.py:263-268`). There is no `repo_id → git-dir` map. Open decision 3 is unresolved in code; integrity is union-search, not per-repo binding.
- Adapter → Card → `task run` is closed: `run_task` reads `config.adapters` and refuses Cards without `execute` (`tasks.py:1174-1181`). `discover_unintegrated()` never writes Cards (`capability/probe.py:19-36`). `install_tool()` does not write Cards (`tooling.py:282-313`). PATH execute fail-closed is real.
- Host compile → doctor → `apply_supervised_wave` is closed: `assert_projections_allow_mutation()` no-ops when `not report.compiled` (`host/doctor.py:70-74`); supervision calls it before the lease (`supervision.py:390-392`). Never-compiled does not block apply. Stale compiled does.
- Console summary does not probe git for Proof: `capture_workspace_read_snapshot()` uses `inspect_integration=False` (`observations.py:281-285`), which forces `inspect_proofs=False` (`snapshot.py:205-206`), and hard-codes `proof_inspection="not_inspected"` (`observations.py:358`). `test_summary_capture_does_not_evaluate_proofs` locks this.

## Source Evidence Accuracy

Source wins over the plan’s module map. Several plan names are stale; behavior is not.

| Plan name | Source |
| --- | --- |
| `proof/verify.py`, `proof/store.py` | `evaluate.py` + `project.py`; no store |
| `hostproj/` | `host/` |
| `capability/{migrate,registry,attest,cli}.py` | `cards.py`, `store.py`, `probe.py`, `cli.py` |

Accurate against locked decisions:

- Kind closed set is 0.7’s five (`proof/models.py:14-19`).
- `contract_hash` is split: task kinds from attempt `task_contract_sha256` (`derive.py:449-454`); `action_receipt` from Objective `contract_sha256` (`derive.py:117-120`).
- `proof list --task` excludes `action_receipt` (`derive.py:34`, `derive.py:56-57`).
- Identity omits `produced_at` / mtime / now (`models.py:68-70`).
- `ContinuationSnapshot` is still a dead type (`continuation/models.py:268-278`; no constructor call sites). P4 landed on `SchedulerSnapshot.decayed_merge_subjects` (`snapshot.py:70`, `snapshot.py:279-301`). Digest payload stays `schema_version = 1` and omits proofs (`snapshot.py:94-128`).
- `live_merge_evidence()` is unused. Production `BudgetUsage` / `decide_no_progress` are not Proof-wired. That matches the P4 deferral, not a miss.
- Journal / confirmation hash omit `decayed_merge_subjects` (`supervision.py:125-165`). Proofs are not a second PASS.
- `cannot_prove` always re-injects `done, merge` (`capability/models.py:87-90`).
- Host skill forbids execute markers and only prints `` `dyro next` `` (`host/compile.py:317-332`, `host/compile.py:279-287`).
- Line dirty / wrong branch stay `_prepare_merge` errors without `PROOF_DECAYED` (`tasks.py:1926-1947`). Task-worktree dirty stays inside `_valid_review_acceptance` via `_collect_task_heads` (`tasks.py:900-911`, `tasks.py:1154-1158`), which P2 explicitly assigned to `review_verdict`.

Inaccurate or over-claimed:

- Plan P9: `agent add` “改为写 Card”. Source still writes `[adapters.*]` (`cli.py:840-851`, `profile.py:63-76`). Runtime upgrade still works (`cards.py:17-32`, `config.py:254-257`). Dual write path, not a second execute plane.
- Plan P6: export “experimental”. CLI help and `test_proof_cli.py:116` forbid the word. Source has already treated export as 1.0-shaped.
- `host/compile.py:194-209` + `capability/probe.py:64-76`: “proven hook surface” is `Path.exists()` on a workspace-relative path. An empty dir (`tests/test_host.py:199-214`) or `hook_surface = "."` / `"dyro.toml"` proves hook authority. Destination is always `.dyro/host-projections/<host>/deny-hook.json` (`compile.py:335-343`), never `hook_surface`.
- `derive._observe_integration()` writes `extra.integration_state` (`derive.py:339-351`); `evaluate_proof()` does not refresh it (`evaluate.py:66`). Payload `extra` can disagree with `status`.
- `verify_release_gates.py:10-14` is a string-presence gate (`def verify_bundle`, `_assert_dependency_integrated`). It cannot see the 3-state or git-dir holes.

## Decision Validity

Do not reopen A1, B1, or authority B. Source does not prove them wrong. It proves several implementations are weaker than those locks.

- **A1 still matches.** Merge / downstream accept-sets are the 0.6 predicates. Proof is a name for those rejects plus attention. `plan_tasks()` does extra Proof I/O (`snapshot.py:280-285`) but does not shrink the ready set. No second gate.
- **B1 still matches on the headed path.** Integrity `live` ≠ workspace `verify` / merge. `--current-heads` is the only decay switch. Bundle refuses git object layout (`bundle.py:88-89`, `bundle.py:235-237`).
- **B1 is not fully implemented for “缺调用方 git 对象”.** Headless proofs can be integrity-`live` with no `--git-dir`. That is an impl gap, not a reason to reopen B1.
- **Authority B still matches the compile/doctor shape.** No hook → compile succeeds, `authority_projection=skill_only` (`compile.py:208-209`, `tests/test_host.py:163-180`). Missing compiled hook → doctor fail-closed (`doctor.py:154-156`). Never-compiled must not, and does not, block apply.
- **Authority B is not host-enforced.** Deny hook is a Dyro projection file. Nothing installs it at `hook_surface`. `skill_and_hook` is a label over a file the host runtime never loads. That is open decision 1, already answered in source as “proof = exists(); dest = projection tree.” Weak proof is the defect, not the destination choice.
- **0.6.0 vs 1.0 commands (open 2).** `pyproject.toml:7` is `0.6.0`. `proof verify-bundle`, `capability *`, `host compile/doctor` are already shipped. Release gates no-op until version/tag is `1.0.0` (`tools/verify_release_gates.py:50-52`). Architecture can freeze; the package must not be labeled 1.0 until the P1s below are closed.
- **Multi-repo (open 3).** Union `--git-dir` is an implementable 1.0 rule if documented. It is not a per-repo object assembly. Do not claim polyrepo stranger-verify is repo-bound.

## Plan Executability

The train is executable from source, not from the plan’s file list.

- P1–P5, P8–P12a, P13 command surface exist and have tests.
- P7 Console Proof display is the 0.8 stub: field present, always `not_inspected`. That is the locked slide, not a miss.
- P9 `agent add` → Card is not done. Compatible via adapter upgrade. Do not block freeze on it; do not document it as done.
- P6 experimental label is already dropped. Treat export + `schema_version = 1` as the 1.0 shape the tests already freeze (`tests/test_proof_cli.py:110-145`).
- Plan §4 still tells an implementer to create `store.py` / `verify.py` / `hostproj/`. Following the plan file now would fork the tree. Freeze the source map; rewrite the plan names or mark them historical.
- `verify_release_gates.py` is not an architecture proof. A 1.0 tag check that only greps symbols will go green while the P1 contract holes remain.

## Scope And Risk

Authority leaks, second gates, false live — scored against source:

| Risk | Verdict | Evidence |
| --- | --- | --- |
| Proof store as merge truth | Absent | No `store.py`; `merge_task` / `check_dispatchable` / `build_task_readiness` do not read Proof |
| PATH / `tools.json` execute | Closed | Discovery is `discovered_unintegrated`; `run_task` requires audited adapter/Card |
| Never-compiled blocks apply | Closed | `assert_projections_allow_mutation()` returns on `not compiled` |
| Second downstream gate | Closed | `PROOF_DECAYED` is attention only |
| Console summary git/Proof probe | Closed | `inspect_proofs` follows `inspect_integration=False` |
| False integrity `live` | Open (headed path closed; headless path open) | `bundle.py:177-184` |
| Workspace status inside bundle | Open | `export_bundle` serializes evaluated `proof_payload` |
| Missing bind shown as `decayed` | Open | `evaluate.py:98-102` vs Appendix A |
| Signed policy applied to unsigned kinds | Open | `derive.py:413-418` + `bundle.py:220-221` |
| Fake hook surface → `skill_and_hook` | Open | `probe.py:64-76` `path.exists()` |
| Hook claimed as host seatbelt | Residual | Hook never written to `hook_surface`; Core remains the only mutation gate (`designs/delivery-physics.md:419`) |
| `agent add` vs Card | Residual dual path | Runtime still fail-closed |

No layer currently lets Console, host skill, or Proof cache authorize merge / execute / apply. The remaining failures are mislabeled 3-states and over-claimed hook authority, not a new write plane.

## Go/No-Go

**Conditional Go for 1.0 architecture freeze.**

A1, B1 (headed), and authority B (compile/doctor/apply) are source-true and must stay locked. Freeze after the P1 fixes below. Do not freeze while export can show workspace `decayed` next to integrity `live`, while `verify-bundle` can `live` without caller git on headless proofs, or while `hook_surface="."` can mint `skill_and_hook`.

Antithesis (steelman for No-Go): a stranger can open the ZIP, read `"status": "decayed"`, then run `verify-bundle --git-dir` and get `live`; or export only `action_receipt`s and get `live` with no git. That is exactly the “two conclusions mixed / false live” failure mode B1 was written to prevent. I still reject No-Go because merge/ready-set/apply authority is not leaking, and the fixes are local to `export_bundle` / `_integrity_of` / `evaluate_proof` / `_proven_hook_surface`.

Tradeoff tension: requiring `--git-dir` for every integrity `live` makes `action_receipt` bundles awkward (no git substrate). SHA-conditional git is more honest to those kinds and weaker than the CLI/B1 sentence. Pick one sentence and make CLI, ADR, and `_integrity_of` identical. Do not leave all three in force.

## Required Fixes

### P0

None. Merge truth, ready set, PATH execute, and never-compiled→apply are not broken.

### P1

1. **Preserve inconclusive through evaluate.** In `evaluate_proof()` (`evaluate.py:37-66`), if derive extras already record `missing` / `unparseable`, or `_valid_review_acceptance` / `_valid_external_signoff` failed because files/bindings are absent (not because a present bind drifted), return `INCONCLUSIVE`, not `DECAYED`. Keep `False` from a complete 0.6 predicate as `DECAYED`. Add a CLI/evaluate test that mirrors `test_missing_review_binding_is_inconclusive` after `list_proofs()`.

2. **Stop embedding workspace rebind in the portable bundle.** `export_bundle()` (`bundle.py:35-60`) must serialize `status=inconclusive` (and empty `decay_reason` / `observed_at`) or omit those fields. `cmd_proof_export` may keep `list_proofs()` for selection, but the ZIP cannot carry workspace `live`/`decayed`. Stranger `cat proofs/*.json` and `verify-bundle` must not disagree.

3. **Make `verify-bundle` + caller git one rule.** Either (a) empty `git_dirs` ⇒ every proof `inconclusive` / `missing_git` (matches `cli.py:2842` and `tools/verify_bundle_stranger.py:89-93`), or (b) keep SHA-conditional git and change the CLI help + invariant 18 to “缺被引用的 git 对象”. I recommend (a) for freeze: B1’s input pair is Bundle + caller git. Add a headless-proof test that today would go `live`.

4. **Stamp `policy_require_signed` per kind.** `review_verdict` → `require_signed_review`; `signoff` → `require_signed_signoff`; other kinds omit or force `false` (`derive.py:413-418`, `bundle.py:220-221`). A `require_signed_review=true` workspace must not fail `gate_log` integrity for missing keys.

5. **Tighten hook-surface proof.** `_proven_hook_surface()` (`probe.py:64-76`) must reject `.`, directories that are not a declared hook root, and ordinary workspace files (`dyro.toml`, `.dyro/`). Missing and absolute already fail (`tests/test_host.py:163-197`). Add `hook_surface = "."` and `hook_surface = "dyro.toml"` as must-not-write-hook cases. Keep destination in the projection tree (do not reopen B); stop minting `skill_and_hook` from `exists()`.

### P2

6. Document open decision 3 as “union of `--git-dir`; no `repo_id` map” or add `--git-dir repo=path`. Do not claim per-repo stranger assembly.

7. Rewrite plan §4 names to `host/`, `evaluate.py`, `project.py`, `cards.py`. Delete the implication that `store.py` / `verify.py` still need to be created.

8. Either make `agent add` append `[[capabilities]]` (P9 text) or document it as a compatible adapter writer. Dual path is safe today, confusing tomorrow.

9. Refresh `substrate.extra.integration_state` in `evaluate_proof()` or drop it from `proof_payload` so JSON cannot show `integrated` next to `decayed`.

10. Keep package version `0.6.0` until P1.1–P1.5 land. Do not treat `verify_release_gates.py` symbol greps as architecture evidence. Export is already non-experimental in source; say that in the 1.0 train, not in a 0.7 experimental footnote.

---

# Final Arbitration

Arbiter: Cursor（主控；核源后合并，不改写各席原文）
Time: 2026-08-15

## 1. Final Verdict

- May implementation start: **可以修 P0，不可宣称 1.0 可发布**
- Required preconditions: 关闭下列 P0 之后，才允许把包版本升到 `1.0.0` 或对外说 1.0
- Blocking reasons: `verify-bundle` 在缺调用方 git、缺 `proof_sha256`、以及 require-signed 被写成 JSON 布尔时仍可报 `live`；当前包号仍是 `0.6.0`，1.0 发布门禁是字符串扫描且对本版本 skip
- 合入/发布：`阻断发布 1.0`。不阻断继续修完整性。不重开 A1 / B1 / 权威投影 B

席位对照（原文保留在各自签名段）：

| 席位 | 对 1.0 可发布 | 对 A1 merge 真值 |
| --- | --- | --- |
| Cursor | Conditional Go（升号前收 P1） | 成立 |
| code-reviewer | Conditional Go，P0 未关不得打 tag | 成立 |
| security-reviewer | **No-Go** | 成立（本切片未重开） |
| critic | **No-Go** | Go，勿重开 |
| architect | Conditional Go（架构可冻，包不可标 1.0） | 成立 |

终裁取更严的可发布结论：**No-Go for 1.0 releasability**。取全体一致的 merge 结论：**A1 Go**。

## 2. Repo / Module Go-No-Go

| Repo/Module | Spec | Plan | Verdict | Reason |
| --- | --- | --- | --- | --- |
| `src/dyro/tasks.py` merge / dispatch | A1 | P2/P4/P5 | **Go** | 不读 Proof；ready-set 不缩；线 dirty 不标 `PROOF_DECAYED` |
| `src/dyro/proof/evaluate.py` workspace verify | 三态 | P1/P2 | Conditional Go | 缺文件被 0.6 `False` 打成 `decayed`，不伪造 `live`，不改 merge |
| `src/dyro/proof/bundle.py` verify-bundle | B1 | P6/P13 | **No-Go** | 无 pin / 无 digest / JSON `true` 可 `live` |
| `src/dyro/capability/` + `host/` | 权威 B | P8–P12a | Conditional Go | PATH 不能执行；从未 compile 不挡 apply；`skill_and_hook` 是产物标签 |
| 发布 / `pyproject.toml` / gates | 1.0 身份 | P13 | **No-Go** | 版本 `0.6.0`；门禁 skip；publish 未跑 sdist 陌生人核验 |
| 总体 | 交付物理引擎 | P1–P13 | **No-Go（1.0） / Conditional Go（修完整性）** | merge 真值未裂；1.0 句子已假 |

## 3. P0 Required Fixes

源码已复核。下列 P0 成立。未成立的「A1 第二道闸」已驳回。

### P0-F1: 缺调用方 git 对象不得 `live`

Evidence:

- `src/dyro/proof/bundle.py` `_integrity_of`：`shas` 为空则跳过 `MISSING_GIT`，`current_heads is None` 时直接 `LIVE`
- `_has_substrate` 在仅有 `bytes_sha256` / `plan_sha256` / `attempt_id` / `contract_hash` 时为真
- `src/dyro/cli.py` 帮助：「缺省不得报 live」
- 诚实导出：`task-heads.json` 缺失时 `review_verdict` 的 `repo_heads` 可空（`derive.py`）

Decision:

- `git_dirs` 为空 ⇒ 每条 Proof `inconclusive` / `missing_git`，整捆不得 `live`
- 有 `git_dirs` 但该条没有可解析 pin：同样 `inconclusive`（不要用「有字节哈希就算完整性 live」绕过 B1 的输入对：Bundle + 调用方 git）
- 不重开 B1：这不是身份核验

Acceptance:

- 只有 `action_receipt` / 无 `repo_heads` 的 bundle，`git_dirs=()` → 退出码 3，无 `live`
- 现有带 SHA 的陌生人夹具：有 `--git-dir` 仍可 `live`；无 `--git-dir` 不得 `live`

### P0-F2: `proof_sha256` 对每个 `proof_id` 强制

Evidence:

- `verify_bundle`：`if expected and sha256 != expected`；缺字段跳过哈希
- `_valid_manifest` 只查 `kind` + `schema_version`

Decision:

- `_valid_manifest` 要求非空 `proof_ids: list[str]`，且 `proof_sha256` 对每个 id 有非空 hex
- 缺 digest / 不匹配 → `BUNDLE_BYTES_MISMATCH` / `inconclusive`，永不 `live`

Acceptance:

- 删掉某 id 的 digest 再改 `proofs/<id>.json` → 不得 `live`

### P0-F3: require-signed 必须 fail-closed

Evidence:

- `proof_from_payload`：`str(value)` 把 JSON `true` 变成 `"True"`
- `_requires_declared_keys` 只认 `value == "true"`
- 诚实导出写的是 `"true"` / `"false"` 字符串；陌生人/手改 ZIP 用布尔即可绕过缺密钥

Decision:

- 只接受 `true`/`false`（布尔或小写字符串）；其它值 `inconclusive`
- 该 kind 需要签名且 `declared_key_ids` 为空 → `missing_declared_keys`
- `policy_require_signed` 按 kind 盖戳：`review_verdict` ← `require_signed_review`；`signoff` ← `require_signed_signoff`；其它 kind 不得因工作区签复核策略而缺密钥失败（architect P1-4，并入本条，避免修完布尔又误伤 `gate_log`）

Acceptance:

- `{"require_signed_review": true}` + 空 `declared_key_ids` → 不得 `live`
- `require_signed_review=true` 的工作区导出的 `gate_log` 不得因缺密钥而 `inconclusive`

### P0-F4: 不得把本树标成已发布 0.6.0，也不得在 P0-F1–F3 未关时打 `1.0.0`

Evidence:

- `pyproject.toml` `version = "0.6.0"`
- `tools/verify_release_gates.py` 在非 `1.0.0` 时 skip
- 门禁是子串存在，不是行为；`pypi-publish` 的 sdist smoke 不跑 `verify_bundle_stranger.py`

Decision:

- 本树禁止再当 `0.6.x` 发布（已含 Proof / Card / Compiler / `verify-bundle`）
- 包版本保持 `0.6.0` **直到** P0-F1–F3 落地；然后一次升到 `1.0.0`
- `1.0.0` 门禁必须跑：sdist 安装后的 `verify-bundle` 夹具；无 `--git-dir` 不得 `live`；`tasks.py` 的 `merge_task` / `check_dispatchable` 不得引用 `dyro.proof`
- 删掉把 `graph.py` 里 `_assert_dependency_integrated` 当 P5 证据的子串检查

Acceptance:

- 未升号前：`verify_release_gates.py` 对本树若被当成 0.6 发布应失败（或发布作业根本不发这棵树）
- 升号后：缺 P0 行为测试则拒绝 tag

## 4. P1 / P2

### P1（1.0.0 前应收，可与 P0 同 PR）

1. **缺文件 / 不可解析在 evaluate 后仍为 `inconclusive`。** `_valid_review_acceptance` 对缺 `review.md` 返回 `False`，`evaluate_proof` 打成 `decayed`。区分 absent/`None` 与完整谓词失败/`False`。CLI 测试须覆盖 `list_proofs()` 之后，不只 derive。
2. **便携 ZIP 不得携带工作区 rebind 结论。** `export_bundle` 经 `proof_payload` 写入 `status` / `decay_reason` / `observed_at`。陌生人 `cat` 与 `verify-bundle` 会打架。导出时这些字段置 `inconclusive` / 空，或省略。
3. **`skill_and_hook` 不得暗示宿主已加载 deny hook。** 去向仍是投影树（不重开 B，不写 `hook_surface`，除非日后单独立项）。二选一：文案写明「产物写在 SKILL.md 旁，未安装到 hook_surface」；或在安装路径存在前不要发 `skill_and_hook`。收紧 `_proven_hook_surface`：拒绝 `.`、普通文件、`dyro.toml`、非声明的 hook 根。
4. **pin 必须是完整对象 ID。** `_SHA_RE` 现为 7–64；多 `--git-dir` 的 `any(cat-file)` 在短 SHA 下会串库。只接受 40 或 64 hex。帮助写明：调用方备齐对象库；`--git-dir` 是并集，不是 `repo_id` 映射。
5. **README 各国语言写清两套命令。** 身份句不够。陌生人只读 README 必须看到：`--git-dir`、完整性 ≠ merge、缺对象 → `inconclusive`。
6. **A1 用 AST/import 锁死。** `merge_task` / `check_dispatchable` / `_prepare_merge` 不得引用 `dyro.proof`。
7. **删 `*.toml` 留下 SKILL/hook 不得再当「未编译」。** 零投影文件仍放行 apply（锁定兼容）。有产物无有效 manifest → `compiled=True` 且 `TAMPERED`。
8. **Host skill 表格字段转义。** `cannot_prove` 等不得把换行 / 反引号 / 禁止执行标记打进 SKILL.md。

### P2

- `_bundle_inconclusive` 不要伪造 `REVIEW_VERDICT` kind
- Host doctor 范围写明：只挡受监督 `apply`，不管 `task run` / `merge`
- `agent add` 仍写 `[adapters.*]`：标明兼容升级，或改写 `[[capabilities]]`
- 计划 §4 文件名改成 `host/` / `evaluate.py` / `project.py` / `cards.py`
- `integration_state` 与 `status` 不得打架
- `force_inconclusive` 恒真：删或做实
- ZIP 成员大小上限
- `cryptography>=50.0.0`（CVE-2026-69247；本树只用 Ed25519，可达性低）
- 证据 ZIP 若同时带 `manifest.json` 与 evidence 标记：fail-closed
- JSON 可加 `conclusion: integrity` / `merge_equivalent: false`，防只看 `status` 的脚本

驳回 / 降级：

- 「A1 已破 / Proof 成第二道 merge 闸」：源码不支持
- 「必须把 deny hook 写进 `hook_surface`」：会扩大变异面，重开 B；不当 P0
- 「短 SHA / 并集 `--git-dir` 是身份洞」：B1 本就不是身份；收紧到 P1
- 「从未 compile 放行 apply 是漏洞」：锁定兼容；只修「删 toml 逃逸」

## 5. Open Micro-Decisions

1. **deny hook 只写投影树：** 去向正确，标签过满。假权威在 `skill_and_hook` 与 `exists()` 证明，不在「没写到 hook_surface」。保持去向；改标签或文案。
2. **`0.6.0` vs 1.0 能力：** 两个方向都是发布谎言。本树不得当 0.6.x 发；也不得在 P0 未关时标 1.0。版本号先不动，修完 P0 再升。
3. **可重复 `--git-dir`：** 机制够用。1.0 必须写「调用方备齐对象；并集查找；不是 `repo_id` 绑定」。不要宣称多仓陌生人核验是按仓装配。

## 6. Instructions For The Execution Agent

先改 `bundle.py` + 测试，再动 host/evaluate/README，最后才碰 `pyproject.toml` 版本号。

Must close:

- P0-F1 空 `git_dirs` / 无 pin → 不得 `live`
- P0-F2 每个 id 强制 `proof_sha256`
- P0-F3 require-signed 解析 + 按 kind 盖戳
- 对应单测：无 git 的 headless bundle；删 digest 后改 JSON；JSON `true` + 空密钥

Do not:

- 改各审查员签名段
- 让 `merge_task` / `check_dispatchable` 读 Proof
- 把 deny hook 写进 `hook_surface`（除非用户单独立项）
- 现在就把版本升到 `1.0.0`
- 把 `git revert` 当祖先断裂
- 新写设计/ADR 文档；测试与必要 CLI 帮助除外

Write back:

- 每个 P0：closed / open
- 新增测试路径
- 是否仍保持 `version = "0.6.0"`

Validation:

```text
cd dyroengineeringflow
uv run pytest tests/test_proof_bundle.py tests/test_proof_cli.py tests/test_proof_decay.py tests/test_host.py tests/test_capability.py -q
uv run python tools/verify_bundle_stranger.py
```

## 7. Conditions To Start Implementation

用户明确说「按终裁修」或「继续修 P0」即可开工。未说之前不要改业务代码、不要 commit、不要升版本。

## 8. Requires Human Verification

- 任一受支持宿主是否会自动加载 `.dyro/host-projections/**/SKILL.md` 或 `deny-hook.json`
- 是否有 1.0 消费者只看 `status==live`、忽略 `mode=integrity`
- 本树相对已发布 `0.6.0` sdist 的 bitwise merge 对错（控制流已核，二进制 diff 未做）
- `objective apply` 是否仍是他们对外说的唯一「自动变异」入口

Final signature: Cursor

