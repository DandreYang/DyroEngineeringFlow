# 交付物理学设计与实施计划对抗式复核审查委员会

日期：2026-08-15

范围：

- 仓库：`/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow`
- 模块：Core Physics 投影、Capability Plane、Host Compiler、续航快照、merge / 下游释放
- 基线：已发布 `0.6.0` 语义；本列车目标 `0.7` → `1.0`

审查材料：

- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/docs/designs/delivery-physics.md`
- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/docs/adr/0006-delivery-physics-and-capability-plane.md`
- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/plans/delivery-physics-implementation.md`
- `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/docs/architecture.md`

SSOT（源码优先于文档）：

- `src/dyro/tasks.py`（`merge_task`、`_valid_review_acceptance`、`_assert_dependency_integrated`、`_prepare_merge`）
- `src/dyro/continuation/models.py`、`snapshot.py`、`budgets.py`、`planner.py`
- `src/dyro/config.py`、`profile.py`、`tooling.py`、`terminology.py`、`cli.py`
- `src/dyro/evidence.py`、`graph.py`

评审团与模型映射（审查员身份 ≠ 产品对标）：

- Codex：具名模型不可用，改派 Auto（`inherit`）— 源码契约、merge / review / 续航同真值
- Grok：`cursor-grok-4.6-xhigh-fast` — 决策有效性、fail-closed、品类与 A1/B1 承重
- Agy：Gemini / Sonnet 额度耗尽，改派 `composer-2.5-fast` — 计划可执行性、CLI、版本列车、落地地雷
- Cursor：本机主控独立段 — 三份文档互洽与实现入口

固定决策（除非源码证明其错误，否则不得重开）：

- 产品身份：本地优先的多仓交付物理引擎，不是 agent / 舰队 / skill 超市。
- 权威投影锁定为 B。
- **A1**：`0.7` 衰减是现有 merge / 下游绑定检查的投影；对错同真值，只多 `PROOF_DECAYED`。
- **B1**：`verify-bundle` = Proof Bundle + 调用方 git 对象；核验完整性，不核验身份。
- 不在仓库内保存「我们学了谁 / 对标谁」的对照附录。
- 可以写 Codex / Claude / OpenCode / Skills；不要写成某编排器的开源替代。

开放微决策（请表态，不要另开产品线）：

1. `0.7` 的 `proof verify` 默认是否只做衰减+绑定重算（文档已写是），源码侧有无必须先重跑 gate 的既有契约？
2. P7 Console 是否必须进 `0.7` 出口，还是可延到 `0.8` 而不破坏 A1？
3. 宿主投影默认只写当前工作区（文档已写是）是否与现有 `dyro agent` / tool 发现路径冲突？

## 规则

1. 审查员只写自己的签名章节。他人不得改写、缩写或润色该章。
2. 源码、现有 schema、现有测试优先于设计稿和计划。
3. 不可由当前源码或材料证实的项目标为 `须人工核`。
4. 发现使用 P0 / P1 / P2。每条必须有路径证据，并尝试反证。
5. 禁止只用同一种文本搜索作为验证。优先打开函数、测试和状态机。
6. 不报告纯文风偏好。优先：错误 merge、假 live、第二份真源、权限扩大、1.0 核验物撒谎、计划不可执行。

---

# Codex Review Section

Reviewer: Codex
Time: 2026-08-15
Verdict: Conditional Go

## Contract Consistency

**P0 · P5 把下游释放接到 `_valid_review_acceptance` / decayed review，与 0.6 真源分裂。**
证据：`plans/delivery-physics-implementation.md:105` 标题「merge / 下游释放拒绝 decayed review」；同文件 `184-187`「task merge 与下游释放仍走现有 `_valid_review_acceptance` / `_assert_dependency_integrated` / 开发线 dirty 预检」。
源码下游只要求 `status == done` + 记录 HEAD 仍是线 HEAD 祖先，不重验 review：

- `src/dyro/tasks.py:552-556` `check_dispatchable`（`run`/`claim` 入口）
- `src/dyro/tasks.py:604-605` `plan_tasks` → snapshot + readiness
- `src/dyro/continuation/snapshot.py:167-186` `_integration_state` 只调 `_assert_dependency_integrated`
- `src/dyro/continuation/planner.py:137-154` `integration_state != "integrated"` → `TASK_INTEGRATION_PENDING`
- `src/dyro/continuation/store.py:966-973` Objective 完成同门

`done` 是粘滞状态文件；`test_tasks.py:151-169` 只锁 **merge** 必须重验 binding，证明 0.6 已把「状态字 ≠ 放行」限定在 merge，而不是下游。
影响：若按 P5 字面给下游加上 decayed review 拒绝，ready set 会比 0.6 更窄，直接违反锁定 A1。
自驳：若「/」意为 merge 走 review、下游走 ancestor，则 P5 正文仍把三个检查并列写在「merge 与下游」主语下，实现者会做成第二套门。定律 II（`docs/designs/delivery-physics.md:86`）「HEAD 移动 / 工作区变脏 / attempt 替换 → PASS 失效」若套到下游，同样加严。
修复：下游只投影 `_assert_dependency_integrated`；`review_verdict` decayed 只许进 merge 人话 / `PROOF_DECAYED` attention，不得改 `build_task_readiness`。
验收：夹具「`done` + `review.md` 被撕 / 任务仓 HEAD 已漂，但 `task-heads.json` 仍是线祖先」→ 下游仍 ready；仅 ancestor 失败 → 仍只报 `TASK_INTEGRATION_PENDING`。

**P0 · 锁定文案「不加严任务仓 dirty（A2 已否决）」与定律 II、与 merge 真源三方打架。**
证据：设计 `delivery-physics.md:8-9,206`；计划 `158,271`；ADR `0006:35`。定律 II 同文件 `86` 又写「工作区变脏 → PASS 失效」。
源码：任务仓 dirty（HEAD 未变）**已经**拒绝 local merge：

```905:909:src/dyro/tasks.py
        dirty = require_ok(
            git(destination, "status", "--porcelain=v1", "-uall"), f"读取 {repo_id} 任务 worktree 状态"
        ).stdout.strip()
        if dirty:
            raise DyroError(f"任务 worktree 不干净，必须先提交全部改动：{destination}")
```

调用链：`_collect_task_heads` ← `_assert_task_heads_current:989-995` ← local `_valid_review_acceptance:1152-1156` ← `merge_task:2057-2059`；`_prepare_merge:1931` 再查一次。开发线 dirty 是另一道硬编码预检（`1937-1939`），与 `require_clean_merge` 运行时开关无关（该旗只能为 true，`config.py:216-217`）。
影响：按 A2 字面「0.7 不拒绝任务仓 dirty」会**放松** 0.6，违反 A1「同真值」。P2 验收同时要求「同真值夹具绿」和「dirty 不做成 merge 拒绝」（计划 `160` vs `158`），夹具无法同时成立。
自驳：「不加严」可被善意读成「不要再叠一层 Proof 门」。但 §7 把「任务仓 dirty 也拒绝 merge」标成已否决的新规则，这是对当前物理的假描述，不是「保持原样」。
修复：删除 A2 否决句；写明 0.6 已拒绝任务仓 dirty；0.7 保持拒绝；Proof 可投影为 `decayed`/`inconclusive`，不得改为 accept。定律 II 的「脏」限定为 **merge/review-acceptance 路径的任务仓 dirty + 开发线 dirty**，不得延伸到下游。
验收：新增锁夹具：`done` + 任务仓 dirty + HEAD 未变 → merge reject，错误集与 0.6 相同。

**P1 · P4 把 proofs 写进未使用的 `ContinuationSnapshot`，并暗示 journal 持久化，制造第二真源。**
证据：计划 `177-178`。`ContinuationSnapshot`（`models.py:267-277`）只在 `continuation/__init__.py` 导出，生产采样/计划用的是 `SchedulerSnapshot`（`snapshot.py:52-73,189-290`；`planner.py:248`）。Scheduler 快照每次重建，不进 Objective journal。`SchedulerReadProjection` 硬锁 `schema_version == 1`（`models.py:238-239`）。
影响：按 P4 改死类型则 proofs 进不了 planner；若再 bump projection schema 或把 proofs 写入 journal 当 PASS，会绕过 `review.md` / `task-heads.json`。
自驳：他们可能只是用错了类型名。即便如此，计划未点名必须改的闭集：`ReasonCode`（`models.py:76-95`）、`attention.py:304-322`、`tasks.py:620-632` `_schedule_block_reason`。漏改则 `PROOF_DECAYED` 会落到默认 `WAITING` / 英文裸码。
修复：P4 目标改为 `SchedulerSnapshot`（或独立、可全量重建的 Proof 投影）；journal 不存 proofs；`schema_version` 保持 1，新字段缺省空且不进 merge 真源。
验收：同一 substrate 快照哈希稳定；merge 仍只读原始绑定字段。

## Source Evidence Accuracy

**已核对为真（A1 核心投影）：**

| 文档声称 | 源码 |
| --- | --- |
| merge 不重跑 gate | `merge_task:2053-2063` 无 `run_gates`；gate 只在 `run_task:1403` / `answer_task:1639` / 显式 `dyro task gates`（`cli.py:1419-1422`） |
| 集成 = `git merge-base --is-ancestor`；`revert` 不裂祖先 | `tasks.py:973-986`；revert 仍留后代，`--is-ancestor` 为 0 |
| review 绑定 attempt/plan/receipt/heads | `_valid_review_acceptance:1133-1161` + `provenance.py:569-582` |
| signoff 再绑一层且依赖有效 review | `_valid_external_signoff:1063-1130`；`merge_task:2061-2062` |
| Trigger 用 `next_probe_at`，不进 fingerprint | `models.py:151-157`；`budgets.py:244-246,435-446` |
| `[adapters.*]` 只有 argv | `config.py:41-46,244-252` |
| 未审计命令可发现、不获执行权 | `cli.py:360-381` 只允许写 codex preset；`home.py:373-375`「尚未集成」；`test_hub.py:595-616`；`test_cli.py:126` |
| Console summary 不探 Git | `observations.py:150` `inspect_integration=False`；`test_console_read_model.py:129-159` |

**P2 · 把 `progress_fingerprint` 写成已在跑的物理学。**
证据：设计 `delivery-physics.md:91,213`；计划 `179`。`decide_no_progress` 只出现在 `tests/test_continuation_budgets.py`。生产 `_budget_usage`（`store.py:606-616`）从不设 `no_progress_cycles`（默认 0）。
影响：0.7 若把 live Proof 投影进 `effective_evidence` 并接线 no-progress，会**新开**预算耗尽路径，不是「只多 reason code」。
自驳：纯函数契约本身正确（忽略 trigger）。假的是「已经在调度环里」。
修复：0.7 不要把 Proof 接入生产 `BudgetUsage`；文档改成「已锁的纯函数，生产未接线」。
验收：现有 Trigger 抖动测试仍绿，且 0.7 不新增 no-progress 自动耗尽。

**P2 · `require_clean_merge` 被写成 merge dirty 的运行时开关。**
证据：设计 `206`。旗在加载期锁死 true（`config.py:216-217`）；线脏拒绝是 `_prepare_merge:1937-1939` 硬编码。
影响：实现者可能去做「读配置再 decay」，引入可关的第二路径。
修复：写「线 dirty 预检硬编码；该旗只是 schema 不变量」。

## Decision Validity

锁定项与源码对齐，**不要重开**：产品 = 本地优先多仓交付物理引擎；权威投影 B；B1 = bundle + 调用方 git 对象、完整性非身份；无竞品对照附录；宿主名可用。A1 **核心**（merge/下游对错不变，只多 `PROOF_DECAYED`）成立。源码证伪的是 A1 骑手「不加严任务仓 dirty」和 P5 对下游的扩门，不是 A1 本身。

`verify-bundle` 缺密钥 → `inconclusive` 与现有 trust store（架构 `184-185`）不冲突。`external_bundle` ≠ P6 ZIP（设计 `179`、计划 `148`）成立。

## Plan Executability

P0–P3、P5 正文（若按上面改过）可执行：先派生、纯函数 decay、CLI 默认不跑 gate、merge 仍走现有函数。

不可按原文开写的切片：

1. **P5** — 见上，先改合同再编码。
2. **P4** — 改错类型 + 漏改 ReasonCode 闭集。
3. **P7** — 未对接 C01。若在 `capture_workspace_read_snapshot` 里为 Proof 跑 `merge-base`/HEAD，会打破 `test_console_read_model.py:129-159`，也违反设计 `4`「不改写已发布 Console 权威语义」。
4. **P2 夹具** — 「同真值」与「dirty 不拒绝」互斥，必须先改验收句。

## Scope And Risk

0.7 出口写死 P1–P7（计划 `294`）。P7 是展示，不是 A1 交付门。P6 把 1.0 B1 命令提前进 0.7 可接受（schema 仍到 P13 才锁），但不要把 P7 绑死在 A1 上。

Host Compiler（0.9）与现有发现面并行，不自动冲突，但输入必须收窄，见微决策 3。

## Go/No-Go

**Conditional Go。** A1/B1/B 方向对；当前文本有两处若照做会改 0.6 对错。先改合同，再开 P2/P5。未改之前 **No-Go for P2/P5 implementation**。

### 微决策建议

1. **`proof verify` 默认必须只做 decay + 绑定重算，不重跑 gate。**
 源码没有「verify ⇒ 重跑 gate」的控制面契约。`changeset verify` 查脏/分支/HEAD（`changesets.py:134+`）；证据 **导入** 核验 `gates.json` 哈希（`tasks.py:1465`），不在控制机重跑；重跑只存在于 `run_task` / `dyro task gates` / runner 侧 `evidence.build`。默认 `--rerun-procedure` 才跑，且必须 dry-run 或隔离。

2. **Console P7 可以滑到 0.8，不破坏 A1。**
 A1 只约束 merge/下游真值。P7 是只读投影。0.7 出口应改为 P1–P6（或 P1–P5 + P6）。若 0.7 仍做 P7：summary 路径不得新开 git probe；衰减展示走 `not_inspected` 或独立 inspect。

3. **工作区本地 host 投影与现有发现路径不固有冲突，前提是编译器只读已审计 adapter/Card。**
 现有三面：`config.adapters`（执行权，`config.py:244-252`）；`tooling.py` + `registry_home()/tools.json`（`dyro open` / tool 偏好）；`DISCOVERABLE_AGENTS` + PATH（`home.py:40-44,353-375`）。另有实验 dispatch 写 `~/.dyro/local-agent-dispatch/skills/SKILL.md`（`experiments/local_agent_dispatch/paths.py:101-102`）。默认写 `.dyro/host-projections/` 不打架。冲突条件：把 `tools.json`/PATH 当可执行 Card；或 `--user` 覆盖 dispatch/`~/.codex/skills` 且 doctor 双写。P10 必须保持。

## Required Fixes

1. **P0** 重写 P5 / 定律 II 下游句：下游只投影 `_assert_dependency_integrated`；decayed review 不加严 ready set。验收见上。
2. **P0** 删除「A2 已否决 / 0.7 不拒绝任务仓 dirty」。改为「0.6 已拒绝，0.7 保持」。P2 夹具与 `_valid_review_acceptance` 同真值，并补 dirty-after-done merge 锁。
3. **P1** P4 改 `SchedulerSnapshot`（或可重建投影）；不把 proofs 写入 journal；`ReasonCode` + `attention.py` + `_schedule_block_reason` 同步加 `PROOF_DECAYED`（attention / 人话，默认不 block 下游）。
4. **P1** P7 滑到 0.8，或写明 C01：summary 零 git I/O。0.7 出口去掉对 P7 的硬依赖。
5. **P2** 文档收回「fingerprint 已在生产宣布死亡」；0.7 不把 Proof 接入 `BudgetUsage`。
6. **P2** P0 文档切片把架构不变量 14–20 回写 `architecture.md`，避免设计/架构双清单。

未改 1–2 之前，不得合并 P2/P5 实现。

---

# Grok Review Section

Reviewer: Grok  
Time: 2026-08-15  
Verdict: Conditional Go

锁定的 A1 / B1 / 投影 B 方向对，源码也已经有 merge 重绑、下游祖先检查、Ed25519 trust store、外部术语 denylist。当前文本仍不能让 A1 承住 `0.7` 同真值，也不能让 B1 承住「与控制面相同的 `live`」——会做出第二真源、假 `live`、以及 `1.0` 核验谎言。先改契约再开工。

## Contract Consistency

**P0 · A1 的枚举衰减规则 ≠ 现有 merge 谓词（假 `live` / 第二真源）**  
- 严重度：P0  
- 证据：设计 4.2 只写 `review_verdict` / `signoff`：`task HEAD ≠ 绑定 HEAD → decayed`。源码 `merge_task`（`src/dyro/tasks.py:2053-2062`）拒绝集是：`status==done` ∧ `_valid_review_acceptance` ∧（可选）`_valid_external_signoff` ∧ `_prepare_merge`。`_valid_review_acceptance`（`1133-1161`）还检查 receipt SHA、`task-heads.json` SHA、`validate_review_binding`（`attempt_id`/`plan_sha256`，`provenance.py:569-582`）、local 下 `_assert_task_heads_current`。`_valid_external_signoff`（`1063-1130`）再绑 `review_sha256` / attempt / plan，并在策略开启时走 Ed25519。`_prepare_merge`（`1921-1942`）另拒开发线 dirty 与错分支。测试已证明拆掉绑定字段后 merge 失败：`tests/test_tasks.py:151-169`。  
- 影响：按 4.2 实现时，receipt/attempt/plan/signoff 漂移后 Proof 仍可 `live`，merge 拒绝 → CLI/Console 第二真源。若执行者按 P5 标题用 `proof.status` 当门，方向反转，缓存 `live` 可能放行。  
- 反驳尝试：P2 写了「与 `_valid_review_acceptance` / `_assert_dependency_integrated` 同真值」。这两函数仍不含 signoff、line dirty、错分支；且 4.2 枚举比 `_valid_review_acceptance` 更窄。标题与夹具不能互相覆盖。  
- 修复：把 `decay(review_verdict)` 定义为 `_valid_review_acceptance` 的全量投影；`decay(signoff)` 定义为 `_valid_external_signoff` 的全量投影。`False` 必须拆成 `decayed`（绑定/HEAD/哈希变了）与 `inconclusive`（缺文件/缺工具/不可解析）。line dirty / 错分支保持现有 merge 错，禁止标成 `PROOF_DECAYED`。  
- 验收：同一组 `0.6` 夹具上，`proof verify` 的 `live` 集合 = `merge_task` 在「忽略 dirty/branch 之后」的接受集合；receipt 改写、binding 删除、signoff 失绑不得出现 `live`。

**P0 · B1 承不住设计 §11 / ADR 的「与控制面相同」句（`1.0` 核验谎言）**  
- 严重度：P0  
- 证据：设计 §11：`拿着 Proof Bundle 和自己提供的 git 对象，能否独立得出与控制面相同的 live / decayed / inconclusive`。B1 / 设计 4.1：`verify-bundle` 核的是「捆内钉死的 substrate + 调用方 git 对象」。控制面 `proof verify` / merge 用的是**当前工作区** substrate（`tasks.py:2057`、`1133-1161`、`973-986`）。钉死快照上对象存在 ⇒ 多为 `live`；控制面在 line HEAD 已走后 ⇒ `decayed`。二者不可能同函数。`architecture.md:237` 已诚实写成「完整性而不是身份」；§11 把完整性说成与控制面同结论。  
- 影响：P13 CI 用固定 git 夹具会绿，对外仍像「陌生人能复验现在是否完成」。这是假 `live`，不是完整性。  
- 反驳尝试：「完整性结论」= 历史自洽，不是现在能否 merge。ADR 后果段比 §11 谨慎，但 §11 仍是品类判据；P6 验收写「与源机相同的完整性结论」却不定义 current substrate。未改 §11 就不能交货。  
- 修复：删掉「与控制面相同的 `live`」。写成两条命令、两套结论：`verify` = `decay(proof, current_workspace_substrate)`；`verify-bundle` = 完整性（字节哈希 + 钉死 SHA 在 `--git-dir` 可解析）。`verify-bundle` 若要衰减，必须另传 `--current-heads`，缺省只能 `live|inconclusive`，不能假装与 merge 同真值。  
- 验收：同一 bundle，源机 HEAD 已移动时：工作区 `verify` → `decayed`；裸 `verify-bundle` 不得报与 merge 相同的 `decayed`，除非传入当前 heads。

**P1 · P5 标题把下游与 review 重绑焊在一起（A1 加严）**  
- 严重度：P1  
- 证据：计划 P5：`task merge 与下游释放仍走现有 _valid_review_acceptance / _assert_dependency_integrated / 开发线 dirty 预检`。源码下游只走祖先：`check_dispatchable` → `_assert_dependency_integrated`（`tasks.py:552-556`、`973-986`）；`snapshot._integration_state`（`snapshot.py:167-186`）。下游**不**调用 `_valid_review_acceptance`。dirty 只在 `_prepare_merge`，不在下游。  
- 影响：按字面实现会把 review 重绑/dirty 加进下游，`0.7` 比 `0.6` 更严，A1 破。  
- 反驳尝试：「仍走现有」可读成「各走各的」。斜线列举足够让执行者写成一个门。  
- 修复：拆句。merge = `_valid_review_acceptance` + 可选 signoff + `_prepare_merge`。下游 = 只 `_assert_dependency_integrated`。禁止 `if proof.status != live: block downstream`。  
- 验收：done 但未 merge 的依赖：下游仍只因祖先失败；review.md 被掏空不得成为新的下游拒绝（`0.6` 也不会）。

**P1 · 定律 I 与默认 `verify` 冲突（品类裂缝，不是 agent 平台）**  
- 严重度：P1  
- 证据：定律 I：事实须「独立程序、针对钉死 substrate **复现**」。设计 4.1 / 计划 P3：`verify` 默认衰减+绑定重算，不重跑 gate argv。`merge_task` 也从不重跑 gate。  
- 影响：`gate_log=live` 只表示哈希还在，不表示测试还能过。默认 `verify` 是重绑器，不是核验程序。A1 下 merge 安全；`1.0`「可复验事实」被掏空。  
- 反驳尝试：review 的 procedure 就是重绑，默认 `verify` 对 `review_verdict` 成立。对 `gate_log` 不成立。  
- 修复：默认 `verify` 可保持 decay+rebind（开放微决策 1 的唯一不破 A1 的答）。必须写明：`live` = 当前 substrate 上绑定仍成立，不是 procedure 已复现。`gate_log` 未 `--rerun-procedure` 不得叙事成「门禁仍通过」。  
- 验收：文档与 CLI help 区分 `rebind` / `replay`；未 replay 的 `gate_log` JSON 不得出现 `procedure_reproduced=true`。

## Source Evidence Accuracy

SSOT 核对（源码胜文档）：

| 声称 | 源码 | 结论 |
| --- | --- | --- |
| merge/review/signoff 已重绑 | `merge_task` `2053-2062`；`_valid_review_acceptance` `1133-1161`；`_valid_external_signoff` `1063-1130`；`test_merge_revalidates_accepted_review_binding` | 成立。比设计 4.2 更宽。 |
| 下游祖先检查已存在 | `git merge-base --is-ancestor` at `tasks.py:978-986`；planner 用 `integration_state` | 成立。`git revert` 仍是后代，A1 不把 revert 当断裂，与源码一致。 |
| `progress_fingerprint` 已忽略 trigger | `budgets.py:435-446` 只哈希 task_states / integration_heads / decisions / effective_evidence | **函数契约成立，生产未接线**。`_budget_usage`（`store.py:606-616`）从不设 `no_progress_cycles`；`decide_no_progress` 只出现在 `tests/test_continuation_budgets.py`。设计把「已在跑的物理学」说满了。 |
| Ed25519 trust store 在；Proof 模型可能无签名 | `signing.py:68` `.dyro/trust/ed25519/<purpose>`；`config.py:56-58` `require_signed_*`；设计 4.1 Proof 字段无 `signature` / `key_id` / `require_signed_*` | 成立。B1「缺已声明的签名密钥 → inconclusive」在无声明槽时恒为假，fail-closed 是死条款。 |
| `tooling.py` 已发现 opencode / cursor-agent | `tooling.py:104-129`；状态在 `registry_home()/tools.json`（`tooling.py:191`，`hub.py:35-47`） | 成立。这是**用户级启动目录**，不是工作区 Card，也不是审计后的 execute。 |
| `terminology.py` 要求 EXTERNAL denylist | `load_terminology_policy` `92-114`：无 env/文件则报错；文件必须在仓库外 `70-77` | 成立。计划 P0 把禁词写在计划里当说明，CI 仍须外部策略；树内无策略时扫描 fail-closed，不是静默放行。 |
| `task explain` 与调度同真值 | `graph.py:170-225` 只看依赖 `done`，不调用 `_assert_dependency_integrated` | **不成立**。`0.6` 已有「explain 可跑、一跑失败」。P5 已点名，但未钉死必须调用同一函数。 |
| Proof / capability / hostproj 模块 | 仓库无 `src/dyro/proof/` | 成立（尚未落地）。 |
| `examples/polyrepo` 可黄金哈希 | 仅有 `examples/polyrepo/dyro.toml`，无 receipt/review 夹具 | P1 验收目前不可执行。 |
| `contract_hash` substrate | 存在 `task_contract_sha256` 与 Objective `contract_sha256` | 须人工核：Proof 用哪一个。 |

**P1 · Proof 无签名槽，B1 的密钥缺席条款是空操作**  
- 严重度：P1  
- 证据：设计 4.1 字段表；`require_signed_signoff` 路径在 `tasks.py:1076-1081`、`1889-1893`。  
- 影响：未声明密钥的伪造 bundle + 调用方对象库 → `live`。控制面在 `require_signed_*=true` 时会拒。再叠加 §11「相同结论」= 假 `live`。B1「不核验身份」本身可成立，但不能再承诺与控制面同状态。  
- 反驳尝试：身份本就不在 B1。成立；所以必须改承诺，或给 Proof 加 `declared_key_ids` + `policy_require_signed` 快照，缺密钥 → `inconclusive`。  
- 修复：二选一，禁止两句并存。  
- 验收：带 `require_signed_review=true` 的源机导出：bundle 无 key 声明时 `verify-bundle != live`，或文档明确 `live` ≠ 可 merge。

**P2 · `progress_fingerprint` 被写成已落地续航定律**  
- 严重度：P2  
- 证据：见上表。  
- 影响：P4「继续忽略 trigger」在未接线函数上会空绿。  
- 修复：P4 须写清：先把 `decide_no_progress` 接到 `reserve_supervised_objective_action`，再投影 Proof；或承认 0.7 只保证函数契约，不保证 mutation 环。  
- 验收：生产路径上 Trigger 抖动不重置 no-progress 的测试，不得只打纯函数。

## Decision Validity

固定决策不重开。攻击的是「文本能否承住承诺」。

A1 作为策略成立：`0.7` 必须是投影，不能当第二道门。当前 4.2 + P5 标题不能执行该策略。

B1 作为策略成立：捆内不塞对象库、不核身份，避免假完美与体积爆炸。当前 §11 / ADR「与控制面相同」把 B1 说成它做不到的事。`architecture.md:237` 的表述可保留。

投影 B 成立：hook 不是 OS 边界（与 `cwd` 不是沙箱同类）。不评成 agent 平台。

**开放微决策（只答有效性，不扩 scope）**

1. **默认 `proof verify` = decay+rebind only？**  
   必须是。否则 A1 会加严（merge 不重跑 gate）。但这只能叫 `rebind`，不能叫定律 I 的「复现」。`--rerun-procedure` 另出口、须隔离。把默认 `verify` 的 `live` 当成「测试仍过」= 假 `live`。

2. **Console P7 能否滑到 `0.8`？**  
   物理学上能。`0.7` 出口写了 `P1–P7`，要滑必须改出口。对抗约束：P7 **不得**先于 P2/P5 同真值落地，否则只读 UI 先广播假 `live`（违设计不变量 14）。可滑 P7，不可滑 P5。

3. **工作区宿主投影 vs 现有 tool/agent 路径？**  
   必须默认工作区：`.dyro/host-projections/`。`tooling.py` / `tools.json` 在 `registry_home()`（用户级），`dyro open` / `tool install` 是启动目录，不是 Capability Card。Host Compiler 读 `tools.json` 当已审计 Card = 第二能力真源，且会把 PATH 发现写进工作区法律。`--user` 才写 Codex home skills。P10 发现-only 必须保持。

**P1 · `PROOF_DECAYED` 吞掉非衰减拒绝（假 `decayed`）**  
- 严重度：P1  
- 证据：设计 4.2：「衰减若挡住 merge 或下游，生成 `PROOF_DECAYED`」。line dirty 在 `_prepare_merge:1937-1939`，不是 review 死亡。  
- 影响：脏开发线被说成「复核失效」，人被赶去重做 review，merge 其实只要求干净工作区。  
- 修复：`PROOF_DECAYED` 仅当对应 Proof 从 `live`→`decayed`。dirty/branch/push 用现有错误。  
- 验收：仅脏 line、绑定仍成立时，不得出现 `PROOF_DECAYED`。

## Plan Executability

**P1 · P5 可被读成「用 Proof 拒绝」或「只加 reason code」**  
- 严重度：P1  
- 证据：标题 `交付门拒绝衰减` vs 正文 `仍走现有 _valid_review_acceptance` vs 风险表 `merge 仍读原始绑定字段再走 decay`。三句可推出：新谓词、旧谓词、旧+decay 串联。  
- 影响：串联时 decay 不全则双门漂移；新谓词则缓存是第二 PASS（计划自己禁止，标题在引诱）。  
- 修复：P5 唯一允许的代码形状：`merge_task` 仍只调现有函数；decay 在 explain/attention 命名已发生的拒绝。禁止 `if not proof.live: raise`。  
- 验收：diff 中 `merge_task` / `check_dispatchable` 不新增 Proof store 读取。

**P1 · `task explain` 已与调度分叉；P5 未钉死函数**  
- 严重度：P1  
- 证据：`graph.py:181-186` vs `tasks.py:552-556`。P5：`task explain 与调度入口必须和真正挡下游的检查一致`。  
- 影响：用 `proof.status`「对齐」会把第二真源写进 explain。  
- 修复：`explain_task` 对每个 `done` 依赖调用 `_assert_dependency_integrated`（或复用 `integration_state`），不读 Proof 缓存。  
- 验收：未 merge 的 done 依赖：`explain.dispatchable=false`，reason 与 `check_dispatchable` 同一祖先错误。

**P1 · P1「黄金哈希」与 `produced_at` 冲突**  
- 严重度：P1  
- 证据：设计 4.1：`produced_at` 取源文件已有时间戳，缺则空，不伪造。`review.md` 无时间字段；`signoff.json` 有 `signed_at`（`tasks.py:1876`）；signed review JSON 有 `created_at`（`reviews.py:50`）；receipt 通常无。mtime 会进黄金哈希且跨 checkout 不稳。`examples/polyrepo` 无证据文件。  
- 影响：P1 验收一开始就红，或把 mtime 写进身份（违「身份哈希不含现在」）。  
- 修复：`produced_at` 只取记录内字段；`review.md` / receipt 为空。黄金哈希用无时钟字段的身份。夹具用 `tests/` 现有 bound review，不用空的 polyrepo。  
- 验收：两次派生、不同 mtime，身份哈希相同。

**P2 · 现有 evidence ZIP 与 P6 Proof Bundle 易撞名**  
- 严重度：P2  
- 证据：`evidence.py` 已有 ZIP（receipt/gates/heads/provenance）；设计把 `external_bundle` 与 P6 分开。CLI 尚无 `proof`。  
- 影响：执行者复用 `task evidence` 导入路径，会把执行包当 Proof Bundle，或把 git 对象塞进 ZIP（否决 B2）。  
- 修复：P6 新命令、新 media type、拒绝 evidence ZIP 布局。  
- 验收：对 `task evidence build` 的 ZIP 跑 `verify-bundle` → `inconclusive`，不是 `live`。

## Scope And Risk

范围没有滑向 agent 平台。Host Compiler 是收缩投影；Card `can_prove` 为空则输出不能当完成证据。这条保住。

真风险是品类谎言，不是功能不够：

- 第二真源：`src/dyro/proof/store.py` 缓存 + Console/explain 读状态，而 merge 读文件。计划禁止「缓存当第二份 PASS」，未写失效键（源文件哈希/世代）。须人工核执行者是否每次 `list/verify` 全量重派生。  
- 假 `live`：不全的 decay、默认不 replay 的 `gate_log`、无密钥槽的 bundle、`verify-bundle` 对钉死 substrate 报 `live`。  
- `1.0` 谎言：§11 品类句 > B1 能力。P13 冻的是这句话就会把谎言锁进 `1.0.0`。  
- 能力平面分叉：`[adapters.*]`、`tools.json`（用户级）、未来 `[[capabilities]]`、宿主 skill。0.7 不解析 Card 是对的；0.9 若用 tooling 探测当审计，fail-closed 破。  
- `architecture.md` 不变量仍是 1–13；设计 14–20 未进架构。P0 若只合设计/ADR/计划，架构仍说旧话。  
- 双品类名：`architecture.md:225` `delivery control plane` vs 设计 `Delivery Physics`。术语允许两者。不是 agent 平台，但是叙事未锁死。不因此 No-Go。

预演失败（计划未覆盖）：

1. 执行者按 4.2 只比 HEAD，receipt 被换 → Proof `live`，merge 拒。  
2. 执行者按 P5 标题读 `proof.status`，脏缓存放行 merge。  
3. 陌生人 `verify-bundle` 得 `live`，源机 line 已 reset → 以为完成仍在。  
4. `require_signed_*=true` 工作区导出无密钥的 bundle → 外机 `live`。  
5. P7 先于 P5 展示 `live`。  
6. Host Compiler 把 `tools.json` 里的 opencode 写成可执行投影。  
7. P4 把 `proofs[]` 写进 journal 后当下一 tick 的真源，不重算 decay。

## Go/No-Go

**Conditional Go**

不重开 A1 / B1 / 投影 B / 本地优先多仓。不建议做成 agent 平台。

不能 Go：§11 与 4.2 原样冻结则 `0.7` 必出第二真源，`1.0` 必出核验谎言。  
不能 No-Go：策略本身与源码方向一致；缺口在契约完整性和验收，可用下面 Required Fixes 补，不必换方案。

`0.7` 开工门槛：P0 两条 + P5 拆句 + decay 全量投影。  
`1.0` 标签门槛：改掉「与控制面相同的 `live`」；`verify` / `verify-bundle` 分家。  
P7 可改到 `0.8`，但不得早于同真值门。默认 verify=rebind 可接受，必须改 `live` 语义。宿主投影默认工作区，禁止把 `tooling.py` 当 Card 真源。

## Required Fixes

1. **P0** 重写设计 4.2 / P2 / P5：`review_verdict` / `signoff` 的 decay 全量等于 `_valid_review_acceptance` / `_valid_external_signoff`；列出 receipt、`task-heads` 文件哈希、attempt/plan binding、local HEAD、签名策略。`inconclusive` vs `decayed` 表。dirty/branch ≠ `PROOF_DECAYED`。  
2. **P0** 改设计 §11、ADR 后果、P6/P13 验收：B1 只保证完整性，不保证与当前控制面 `live/decayed` 相同。`verify-bundle` 无 `--current-heads` 不得声称衰减结论。  
3. **P0** P5 禁止 `merge_task` / `check_dispatchable` 读取 Proof store。下游只保留祖先检查。  
4. **P1** Proof 模型增加 `declared_key_ids` + 导出时的 `require_signed_*` 快照，或删除「缺已声明密钥 → inconclusive」并禁止 §11 同结论句。  
5. **P1** 定律 I / CLI：默认 `verify` 的 `live` = rebind holds；`gate_log` 未 replay 不得当测试通过。  
6. **P1** `explain_task` 调用 `_assert_dependency_integrated`（或 `integration_state`），不读 Proof。  
7. **P1** `produced_at` 只取记录字段；黄金哈希夹具用 `tests/` bound review，不用空 polyrepo。  
8. **P1** P6 拒绝 `task evidence` ZIP 布局；不把 git 对象放入 bundle。  
9. **P1** P11：投影默认工作区；`tools.json` / PATH 发现 = `discovered_unintegrated`；`--user` 才写用户级 skill。  
10. **P2** P0 把不变量 14–20 写入 `architecture.md`；P4 写明 fingerprint 是否接入 mutation；P7 若延期则改 `0.7` 出口。  
11. **须人工核**：Proof `contract_hash` 对应 attempt `task_contract_sha256` 还是 Objective `contract_sha256`；`proof list` 是否每次重派生。未核前禁止把 store 当展示真源。

---

# Agy Review Section

Reviewer: Agy
Time: 2026-08-15
Verdict: **Conditional Go**

## Contract Consistency

| # | 严重度 | 证据 | 影响 | 反驳尝试 | 修复 | 验收 |
|---|--------|------|------|----------|------|------|
| C1 | **P0** | 计划 P4：`ContinuationSnapshot 增加 proofs[]`（`plans/delivery-physics-implementation.md:177`）。源码里 `ContinuationSnapshot` 在 `continuation/models.py:267-277` **从未实例化**；实际采样用的是 `SchedulerSnapshot`（`continuation/snapshot.py:53-74`），进展指纹用的是 `ProgressFacts`（`continuation/budgets.py:244-265`），且 **未接入** supervision 主路径。 | 实施者明天会在错误类型上挂 `proofs[]`，或双写 digest，破坏 snapshot hash 稳定性与 P4 验收。 | 设计 §4.2 写「planner 构造 Snapshot」可理解为概念快照；但计划 **点名** `ContinuationSnapshot`，与源码 SSOT 冲突，反驳不成立。 | P4 改 SSOT 映射：`SchedulerSnapshot._payload` + `ProgressFacts` 装配点 +（若需要）`SchedulerReadProjection`；**删除或标注** `ContinuationSnapshot` 为 dead type；写明 digest 字段与 `schema_version` bump 位置（`snapshot.py:94` vs `planner.py:201`）。 | 同一 substrate 下 `build_scheduler_snapshot` digest 稳定；旧 journal/无 proof 字段 → 空数组兼容；`test_continuation_budgets` 仍绿。 |
| C2 | **P1** | 计划 P7：`Console / dyro attention`（`:199`）。CLI 仅有 `dyro objective attention`（`cli.py:2469-2474`），无顶层 `attention`。 | P7 验收与 CLI 帮助无法对齐；Console 若等新命令会空转。 | 设计 §7 未写 `dyro attention` 顶层命令；仅计划笔误？但 P7 验收绑定该字符串。 | P7 统一为 `dyro objective attention` + Console read_model；若需 task 级 attention，单列 P7b 与 `task explain` 关系。 | `dyro objective attention <id>` JSON 含 `PROOF_DECAYED`；Console 只读夹具绿；文档/计划去掉 phantom CLI。 |
| C3 | **P1** | 设计 §4.1：`dyro proof export <id>`（`:188`）vs 专家路径 `dyro proof export API-101 --bundle`（`:340`）。P3 未定义 export；P6 未澄清 `<id>` 语义。 | P6 可能实现 proof-id 导出，而 UX/文档期望 task-id 批量导出，或相反。 | `--task` 过滤已在 P3 `proof list`；export 可能默认同 task。但 `:188` 与 `:340` 参数类型仍矛盾。 | P6 锁定一种：`export --task <ID>` 或 `export <proof-id>`；设计 `:340` 改一致；CLI 互斥校验。 | 单任务多 proof 导出行为有表驱动测试；help 与 design 同形。 |
| C4 | **P1** | 版本列车：`0.7.0` 用户可见 `dyro proof *`（计划 `:47`）；`1.0.0` 才「Bundle schema 稳定」（`:50`）。但 **0.7 出口** 要求 P1–P7 绿（`:294`）；P6 含 `verify-bundle` + B1 语义（`:189-195`）；P13 才锁 `schema_version = 1`（`:253`）。 | 0.7 要么提前承诺 1.0 级 bundle 契约（无法 semver 演进），要么 0.7 带 unstable bundle 却用 B1 验收——implementer 不知哪条是真。 | 可称 0.7 bundle 为 preview。但与 ADR B1「1.0 可携带核验」及 P13 冻结重复，preview 与 product 承诺混淆。 | 拆列车：0.7 P3 仅 `list/show/verify`；P6 `export` 可进 0.7；`verify-bundle` + schema=1 归 1.0/P13；或 0.7 出口改为 P1–P5+P7。 | 0.7 tag 检查不含 `verify-bundle` 硬门禁，或文档明确 0.7 bundle 为 experimental。 |
| C5 | **P1** | 计划 P5 标题「交付门**拒绝**衰减」（`:105`）vs A1/ADR「只投影、不加严」（ADR `:35`，计划 `:11`）。 | 工程师可能在 `merge_task` / `check_dispatchable` **新增** decay 拒绝分支，违反 A1。 | 正文 `:183-186` 已写「仍走现有 API」。标题 alone 不应 override——但标题会进 PR 描述。 | P5 改名为「交付门 decay **投影**（A1）」；验收首条引用 `_valid_review_acceptance` / `_assert_dependency_integrated` 集合 **不变**。 | 0.6 夹具 merge/下游 accept/reject 集合 bitwise 相同；仅多 proof 字段与 reason。 |

## Source Evidence Accuracy

| # | 严重度 | 证据 | 影响 | 反驳尝试 | 修复 | 验收 |
|---|--------|------|------|----------|------|------|
| S1 | **P1** | P5 要求 `task explain` 与调度入口一致（`:186`）。`graph.explain_task`（`:162-225`）**不**调用 `_assert_dependency_integrated`；`check_dispatchable` 会（`tasks.py:545-556`）。 | `explain` 报 YES、`task run`/`next` 因未集成 fail——P5 验收必红；P7 Console 若吃 explain 图会撒谎。 | 架构 `:132` 说 explain 解释「为何可调度」——当前本就不含集成；这是 **已知缺口**，计划 P5 正要修。 | P5 在 `explain_task` 或共享 helper 复用 `build_scheduler_snapshot` 的 `integration_state`；JSON 增加 `integration` 原因。 | 依赖 done 未 merge 时 explain `dispatchable=false` 且 reason 与 `check_dispatchable` 同文案。 |
| S2 | **P1** | P1 kind 闭集含 `integration_heads`（计划 `:147`）。设计映射为「依赖 HEAD 已是线 HEAD 祖先」（`delivery-physics.md:177-178`）——**非持久文件**，由 `git merge-base` 即时判定（`tasks.py:973-986`）。 | `derive.py` 无稳定 `subject`/`substrate`/`produced_at` 规则；P1 黄金哈希不可定义。 | 可派生为 synthetic proof，substrate=当前 line HEADs + 依赖 task-heads。但计划未写，generation 与 id 公式缺失。 | P1 增「derive 算法」小节：`integration_heads` 的 id、何时 materialize、缺 git → `inconclusive`；与 `_assert_dependency_integrated` 同 git 调用。 | 表驱动：integrated/pending/not_inspected 三态与 scheduler 一致。 |
| S3 | **P1** | P1 `gate_log`：本地 gate 写 `gate-{n}.log`（`tasks.py:1318-1322`）；外部证据为 `gates.json` + `gates/*.log`（`evidence.py:102-111`, `tasks.py:1513-1515`）。计划未列路径。 | 漏派生一半执行模式；或误读 ledger 为 gate 真源（ledger 非绑定证据）。 | 设计 `:174` 写「gate 日志 + receipt」——可含两者。 | `derive.py` 明确：local 扫描 task 目录 + evidence generation；argv 哈希取自 task.toml gate 定义；**不**把 ledger 当 PASS。 | local/external 夹具各派生一条 `gate_log`；argv 变更 → decayed 展示，merge 仍不受影响（A1）。 |
| S4 | **P2** | P1 含 `action_receipt`（计划 `:147`）。Receipt 存 Objective 目录 `action-receipts/`（`action_journal.py:26`），**非** task 目录。 | `dyro proof list --task` 是否应含 objective receipt？implementer 会扫错树或漏 kind。 | 设计 `:178` 列 Continuation Action receipt——属 Objective 面。 | 明确：`proof list --task` **不含** action_receipt；`--objective` 或 0.8+ 再暴露；P1 可先 derive 不挂 CLI。 | task 过滤不返回 action_receipt；objective 路径单独测试。 |
| S5 | **P2** | P1 验收：`examples/polyrepo` 黄金哈希（`:152`）。polyrepo 仅 `dyro.toml`（无 task 证据树）。 | P1 PR 无法用 polyrepo 自证；验收空转。 | 计划同句有「现有测试夹具」——polyrepo 非唯一。 | 验收改为 **必须** 引用 `tests/test_tasks.py` 等 evidence 夹具 + 新建 `tests/test_proof_derive.py`；polyrepo 降为 smoke。 | 夹具目录派生 SHA 稳定；polyrepo 仅 `proof list` 不 crash。 |
| S6 | **P2** | A1：`review` 衰减与 `_valid_review_acceptance` 同真值（`tasks.py:1133-1161`）。本地还调 `_assert_task_heads_current`（含 **task worktree dirty** 拒绝，`:905-909`）。设计 A1 说不加严 **merge** 对「task dirty HEAD 不变」——与 review 绑定检查是两条线。 | decay 若只 mirror merge 预检会 **under-decay** review；若 over-decay 会违反 A1。 | merge 前也调 `_valid_review_acceptance`（`:2057`）；task dirty 已挡 merge。decay 应 mirror review 路径，非仅 line dirty（`:1937-1939`）。 | P2 夹具分表：`review_verdict` decay ← `_valid_review_acceptance`；line dirty ← `_prepare_merge`；禁止混为一表。 | 表驱动 P2 测试命名对应源码函数；merge 夹具 0.6 集合不变。 |

## Decision Validity

| # | 严重度 | 证据 | 影响 | 反驳尝试 | 修复 | 验收 |
|---|--------|------|------|----------|------|------|
| D1 | **P2** | 开放微决策 #1：`verify` 默认 decay+rebind。设计 `:192`、P3 `:171` 一致。源码 merge 不重跑 gate（`:2057-2063`）；`_valid_review_acceptance` 做 hash/HEAD 重绑。 | 无冲突；可锁定。 | 试图找「verify 必须 rerun gate」契约——未找到。 | **锁定** 默认 decay+rebind；`--rerun-procedure` 仅诊断。 | P3 默认 verify 无 gate 子进程；`--rerun-procedure` 需 dry-run/隔离。 |
| D2 | **P2** | 开放 #2：Console P7 slip 0.8。计划 0.7 出口含 P7（`:294`）；P7 可并行 P4（`:129`）。 | slip 会简化 0.7，但与 marketing「attention 人话」不一致。 | Console 已有 read_model（`console/read_model.py`），增量可行。 | **建议不 slip**；若 slip，改 0.7 出口为 P1–P5 并同步 ADR 后果。 | 文档、计划、ADR 三处版本表一致。 |
| D3 | **P2** | 开放 #3：workspace-local host 投影 vs `tool`/`agent` 路径。P11 `:232-233` 已锁 workspace 默认；`home.py`/`tooling.py` 仍管 discover/launch（`:353-413`）。 | 0.7–0.9 无冲突；P10 前 dual surface 可接受。 | Host Compiler 0.9 才写盘；0.7 不碰。 | P11 明确：`host compile` 读 `config.adapters` + `capability test`；**不**写 `~/.codex/skills` 除非 `--user`。 | P11 验收 doctor `scope=workspace|user`。 |
| D4 | **P1** | 设计 §4.2：`progress_fingerprint` 纳入 live Proof（`:213`）。`ProgressFacts` 仅测试使用（`test_continuation_budgets.py`）；`progress_fingerprint` 不含 proof 字段（`budgets.py:439-445`）。 | P4 若只改 `ContinuationSnapshot` 则 **完全不生效**。 | 设计意图是投影进 `effective_evidence`/`integration_heads`，非新层——与 S1/C1 同源。 | P4 在 **ProgressFacts 装配点**（新建，likely supervision/planner 交界）投影 live proof digest；**不**改 fingerprint 输入集合语义。 | Trigger 抖动不改变 fingerprint；merge 相关 live→decayed 会改变 fingerprint（与现有 delivery 事实一致）。 |

## Plan Executability

**P1 明天能否开工：** 可以，但必须先消 C1、S2、S3 的 derive 规格，否则 `proof/models.py` + `derive.py` 会猜。

| Phase | 可执行性 | 阻塞 |
|-------|----------|------|
| P1 | 条件可执行 | `integration_heads`/`gate_log` 映射、polyrepo 验收 |
| P2 | 依赖 P1 id/substrate | 夹具必须钉 `_valid_review_acceptance` / `_assert_dependency_integrated` |
| P3 | 清晰 | 新 subparser；无现有 `proof` 冲突 |
| P4 | **高风险** | C1/D4：改错 snapshot 类型 |
| P5 | 清晰但易做错 | S1 explain 缺口；C5 标题误导 |
| P6 | 条件 | C3/C4 CLI 与版本列车 |
| P7 | 条件 | C2 attention 路径；`ReasonCode.PROOF_DECAYED` 需进 `attention.py` 分类（`:305-319` 无 hook） |

**PR 依赖图：** P2∥P3 可行；P7∥P4 可行——但 P7 依赖 P4 reason code 与 attention 映射，不是纯 UI。

**0.7 不含 Card/Compiler：** 与 `config.py` adapters-only（`:244-252`）一致；`tooling.py` 发现链 **不** 进 0.7 执行面——符合 ADR。

## Scope And Risk

| 风险 | 严重度 | 说明 |
|------|--------|------|
| 双 registry（`TOOL_DEFINITIONS` vs `config.adapters`） | P2（0.8） | `home.py:40-44` 与 `config.adapters` 并行；0.7 不迁移，但 P7 Console 勿把 tool READY 展示为 proof/gate 通过 |
| Proof store 变第二 PASS | P1 | 计划 `:88` 已禁；merge 必须仍读 `review.md` 绑定（`tasks.py:2057`） |
| Snapshot schema break | P0 | 错改 `SchedulerSnapshot` payload 会波及 objective tick hash、Console digest |
| `verify-bundle` 身份越界 | P2 | B1 已锁；缺 git → `inconclusive`（设计 `:192`）——与 signing 域分离，implementer 勿复用 `verify_record` |
| Terminology P0 | P2 | `terminology.py` 强制外部策略（`:114`）；P0 合并 doc 不碰代码——CI 须外置 denylist，计划 `:141-143` 正确 |

## Go/No-Go

**Conditional Go：** P1 可启动，但在改代码前必须完成计划文档级修正（C1/C2/C4/S2/S3）与 P4 目标类型澄清。否则高概率在错误 snapshot、错误 CLI、错误 derive 源上浪费 0.7 第一个 PR。

A1 / B1 / Projection B **未被源码反驳**；衰减与 merge 真值源在 `tasks.py` 已存在，Proof 层应是投影而非第二道门——计划正文对此一致，但 P5 标题与 P4 类型命名构成实施噪声。

## Required Fixes

1. **P0** — 重写 P4 目标：`SchedulerSnapshot` + `ProgressFacts` + `ReasonCode.PROOF_DECAYED`；废弃或迁移 `ContinuationSnapshot` 计划表述（C1, D4）。
2. **P0** — 发布 derive 规格附录：`gate_log` / `review_verdict` / `signoff` / `integration_heads` / `action_receipt` 各 kind 的源路径、id 公式、substrate、`produced_at` 规则（S2, S3, S4）。
3. **P1** — 统一 proof export CLI 与 design `:188`/`:340`（C3）。
4. **P1** — 澄清 0.7 vs 1.0 的 `verify-bundle` 与 bundle schema 边界（C4）。
5. **P1** — P5 增 `explain_task` 集成检查或显式引用 scheduler integration_state（S1）；rename P5 标题（C5）。
6. **P1** — P7 改为 `dyro objective attention`；`attention.py` 为 `PROOF_DECAYED` 指定 `AttentionKind`（C2）。
7. **P2** — P1 验收绑定真实 test fixtures，弱化 polyrepo（S5）。
8. **P2** — 锁定微决策 #1（D1）；#2/#3 写入计划决策表，避免 implementer 自行解释（D2, D3）。

---

# Cursor Review Section

Reviewer: Cursor（主控独立段；填写时未见 Codex / Grok / Agy 结论）
Time: 2026-08-15
Verdict: **Conditional Go** — 品类、A1、B1、投影 B 可执行；同一份设计里定律 II 仍用未限定的「失效」，实现者会做成 A2。

## Contract Consistency

三份文档在列车、CLI、kind 闭集、verify 默认行为上已对齐。残留合同裂缝在**定律层 vs 锁定层**：

### P1-C1：定律 II 仍把「脏工作区 / gate argv 变」写成无条件失效

**证据：**

- 设计文首与 §4.2 锁定 A1：`0.7` merge / 下游与 `0.6.0` 同真值；任务仓 dirty 不加严；`gate_log` 展示 decayed 但不新拒绝 merge。
- 同一文件定律 II（约 L86–L87）仍写：「PASS review 在 task HEAD 移动、**工作区变脏**、或 attempt 被替换后失效」「gate receipt 在 **gate argv**、仓库内容或合约哈希变化后失效」。
- 源码 `merge_task`（`src/dyro/tasks.py:2053-2063`）只重验 `_valid_review_acceptance` 与可选 signoff，**不**重跑 gate。
- `_valid_review_acceptance`（`tasks.py:1133-1161`）比的是 receipt/heads 哈希与绑定，外加本地 `_assert_task_heads_current`。任务仓 porcelain dirty 且 HEAD 未变时，现有 merge 仍通过。
- `_prepare_merge`（`tasks.py:1937-1939`）拒绝的是**开发线** dirty，不是任务仓 dirty。

**影响：** 先读定律再读 §4.2 的实现者，会把 A2 做进 `0.7`，破坏 ADR 兼容条。

**反证尝试：** 定律可以表示「物理上已死」，§4.2 只约束 merge 门。若 `proof list` 显示 decayed、merge 仍按旧检查放行，两套语言会同时出现在 CLI 上，用户会以为系统自相矛盾。这不能当反证成功；必须在定律 II 写明「展示衰减 ≠ 新的 merge 拒绝」。

**修复：** 定律 II 四条改成与 §4.2 / A1 同构：哪条影响 merge，哪条只影响展示，哪条是 0.8+。

**验收：** 定律 II 不再出现未限定的「工作区变脏 → 失效」；P2 夹具证明任务仓 dirty + HEAD 不变时 merge 仍与 0.6 相同。

### P2-C2：定律 III 把 review adapter 写成「不是写入者」

**证据：** `read` adapter 必须写出 `review.md`（`architecture.md` L78）。定律 III（约 L98）把 review adapter 与 dispatch / Console 并列「都不是写入者」。

**影响：** 实现者可能禁止复核进程写 `review.md`，或把复核误判为开发线写入者。

**反证：** 在「开发线 / 任务源码」语义下，复核确实不是线写入者。但文档没写这个限定。

**修复：** 改成「不是开发线或任务源码的写入者；复核只写 `review.md`」。

## Source Evidence Accuracy

已用函数体核对、不是只搜字符串：

| 文档声称 | 源码 | 结论 |
| --- | --- | --- |
| merge 重验 PASS + 当前 HEAD | `merge_task` + `_valid_review_acceptance` | 成立 |
| 下游要祖先，不只看 `done` | `_assert_dependency_integrated` 用 `merge-base --is-ancestor`；continuation snapshot 采样 integration | 成立 |
| `explain_task` 与调度同真值 | `graph.py:181-186` 只看依赖 `done`，不看祖先 | **文档 P5 已要求修 explain**；源码现状仍裂 |
| `progress_fingerprint` 忽略 Trigger | `budgets.py:435-446` 只哈希 task_states / integration_heads / decisions / effective_evidence | 成立 |
| `0.7` 无 proof/capability/host CLI | `cli.py` 仅有 `console` / `agent` / `tool`，无这三项 | 成立，计划是新增 |
| 术语策略在仓库外 | `terminology.py:98-114` | 成立；P0 已改成外部策略 |
| tool catalog 已发现 opencode | `tooling.py` `TOOL_DEFINITIONS` | 成立；P10 已写不得升 execute |

### P1-C3：`explain` 与 `run` 对下游集成的合同仍裂

**证据：** `explain_task` 在依赖为 `done` 时不调用 `_assert_dependency_integrated`。`run_task` / continuation snapshot 会挡。计划 P5 已写要修。

**影响：** 0.7 若只投影 Proof 到 attention、不改 explain，用户仍会看到「可调度」然后一跑失败。这不是 A1 加严，是把已有裂口继续展示。

**反证：** A1 说对错不变。explain 今天就错，保持不变也符合字面 A1。但 P5 自己要求修 explain，所以这是计划内必做，不是新范围。

**验收：** `task explain` 在依赖 done 但未进线时给出与 `TASK_INTEGRATION_PENDING` 同类原因。

## Decision Validity

A1 / B1 / 投影 B 与源码相容，不重开。

开放微决策（Cursor 表态）：

1. **`proof verify` 默认衰减+绑定重算。** 源码没有任何「verify 必须重跑 gate」的契约。merge 也不重跑 gate。重跑是可选诊断，不是 0.7 门。
2. **P7 可滑到 0.8。** A1 的承重是 merge / 下游真值，不是 Console。`0.7` 出口用 `dyro proof list` + attention 即可。Console 只读展示不破坏 A1，但会扩大 0.7 表面积。
3. **工作区本地宿主投影与现有 agent/tool 路径不冲突。** `dyro agent add` 写的是 Profile adapter；`tool list/install/open` 不写 skill 目录。默认不写 `~/.codex` 是对的。

### P2-C4：操作格未映射已有 mutation

`task claim` / `evidence import` / `answer` / `changeset` 已是写动作，格子里没有。0.7 不实现格子也能做 Proof。不挡 P1。应在 0.8 Card 前补映射，避免 host deny hook 漏拦。

## Plan Executability

P1–P5 在 A1 下可开工：派生五种 kind、纯函数 decay、默认 verify 不重跑、fingerprint 不双计、P5 投影现有 API。

P6 的 B1 形状已可写测试：有 git 夹具 → 与源机同结论；无 git 对象 → `inconclusive`。

### P2-C5：P7 进 0.7 出口偏肥

计划 §9 仍写「0.7 = P1–P7」。与微决策 2 冲突。建议 0.7 = P1–P5（+可选 P3 CLI），P6 可并行但不挡 0.7 身份，P7 延到 0.8。**须人工核**产品是否坚持「0.7 就要能在 Console 看见 Proof」。

## Scope And Risk

- 最大剩余风险：定律 II 把 A1 从实现者手里抢回去。
- 次风险：`proof list` 显示 `gate_log=decayed` 而 merge 仍过，若无人话解释，会被当成 bug。
- 1.0 B1 不会撒谎，只要 CI 夹具带 git 对象、缺对象测 inconclusive。
- 未发现把 dispatch / 视频升成 Proof 的回潮。

## Go/No-Go

**Conditional Go** 开 P1 代码。前置：只改文档，把定律 II（及定律 III 的写入者句）收成与 A1 同构。不改产品方向，不重开 A1/B1/B。

## Required Fixes

1. **P1-C1** 定律 II 与 A1 同构（必须先于 P1 实现）。
2. **P1-C3** P5 落地时修 `task explain`（已在计划，保持）。
3. **P2-C2** 定律 III 限定「源码写入者」。
4. **P2-C4** 0.8 前补操作格映射。
5. **P2-C5** 决定 P7 是否离开 0.7 出口。

---

# Final Arbitration

Arbiter: Cursor（主控）
Time: 2026-08-15

## 1. Final Verdict

- May implementation start: **Conditional Go — 只许先改文档契约；业务代码 No-Go，直到下列 P0 关闭。**
- Required preconditions: 设计 / ADR / 计划把衰减、下游、dirty、P4 类型、`verify`/`verify-bundle` 分家写到与源码同构；再发 derive 规格附录。
- Blocking reasons: 按原文实现会改 `0.6` 对错（P5 扩下游、A2 骑手放松 dirty），或做出假 `live`（§4.2 窄枚举、§11 同结论句），或改错快照类型。

四人均为 **Conditional Go**。品类、A1 核心、B1、投影 B **不重开**。源码证伪的是骑手与计划用词，不是策略本身。

## 2. Repo / Module Go-No-Go

| Repo/Module | Spec | Plan | Verdict | Reason |
| --- | --- | --- | --- | --- |
| 设计 `delivery-physics.md` | 定律 II / §4.2 / §11 与 A1/B1 打架 | — | **No-Go 冻结** | 先改契约 |
| ADR-0006 | 决策 9 骑手假描述 dirty；后果段「相同完整性结论」易读成同 `live` | — | **Conditional Go** | 改骑手与 §11 对齐句 |
| 计划 P1 derive | 缺 kind 源路径 / id 公式 | 验收绑空 polyrepo | **Conditional Go** | 附录齐了才能写 `proof/` |
| 计划 P2/P5 | — | 标题扩门；dirty 夹具互斥 | **No-Go 实现** | 未改合同不得合入 |
| 计划 P3 CLI | verify=rebind 已齐 | 无 `proof` 冲突 | **Go**（文档 P0 后） | 默认不跑 gate |
| 计划 P4 | 点名死类型 | journal/schema 暗示 | **No-Go 实现** | 改挂 `SchedulerSnapshot` |
| 计划 P6/P13 | B1 策略对 | 0.7 出口绑 `verify-bundle` | **Conditional Go** | export 可进 0.7；schema=1 与硬核验归 1.0 |
| 计划 P7 Console | 非 A1 门 | phantom `dyro attention` | **滑到 0.8** | 见微决策 2 |
| Card / Host Compiler | 0.8/0.9 | 不进 0.7 | **Go 保持** | 与 `config.adapters` 一致 |
| `src/dyro/proof/` 业务代码 | — | — | **No-Go** | 文档 P0 未关 |

## 3. P0 Required Fixes

### P0-F1: `decay(review_verdict|signoff)` = 全量现有谓词

Evidence:

- 设计 §4.2 只写 `task HEAD ≠ 绑定 HEAD`（`delivery-physics.md:206`）。
- 源码 `_valid_review_acceptance`（`tasks.py:1133-1161`）还查 receipt SHA、`task-heads.json` SHA、`validate_review_binding`（attempt/plan）、local `_assert_task_heads_current`。
- `_valid_external_signoff`（`tasks.py:1063-1130`）再绑 review/heads/attempt/plan，策略开启时走 Ed25519。
- Grok 签名段 P0；主控复核函数体成立。

Decision:

- `decay(review_verdict)` := `_valid_review_acceptance` 全量投影。
- `decay(signoff)` := `_valid_external_signoff` 全量投影。
- `False` 拆 `decayed`（绑定/HEAD/哈希变了）与 `inconclusive`（缺文件/缺工具/不可解析）。
- 开发线 dirty / 错分支保持 `_prepare_merge` 现有错，**禁止**标成 `PROOF_DECAYED`。

Acceptance:

- 同一组 `0.6` 夹具：`proof verify` 的 `live` 集合 = `merge_task` 在「忽略线 dirty/错分支之后」的接受集合。
- receipt 改写、binding 删除、signoff 失绑不得 `live`。
- 仅脏开发线、绑定仍成立 → 现有 merge 错，无 `PROOF_DECAYED`。

### P0-F2: `verify` 与 `verify-bundle` 分家；删「与控制面相同的 live」

Evidence:

- 设计 §11（`delivery-physics.md:428`）与 ADR 后果（`0006:61`）写「与控制面相同的 `live` / `decayed` / `inconclusive`」。
- B1 / §4.1：`verify-bundle` 核的是钉死 substrate + 调用方 git 对象；控制面 `verify`/merge 用当前工作区。
- HEAD 已走后二者必然分叉。Grok P0；`architecture.md`「完整性不是身份」可留。

Decision:

- `proof verify` = `decay(proof, current_workspace_substrate)`。
- `verify-bundle` = 完整性（字节哈希 + 钉死 SHA 在 `--git-dir` 可解析）。无 `--current-heads` 不得报与 merge 相同的衰减结论。
- 删掉品类句里「与控制面相同的 `live`」。可写「相同的完整性结论」，并定义完整性 ≠ 现在能否 merge。

Acceptance:

- 同一 bundle，源机 HEAD 已移动：工作区 `verify` → `decayed`；裸 `verify-bundle` 不得自动报 `decayed`。

### P0-F3: P5 下游只投影祖先检查；merge/dispatch 不读 Proof store

Evidence:

- 计划图 `:105`「merge / 下游释放拒绝 decayed review」；P5 正文把三个检查并列在「merge 与下游」下（`:184-187`）。
- 源码下游只走 `_assert_dependency_integrated`（`check_dispatchable:552-556`，`snapshot.py:167-186`）。不调 `_valid_review_acceptance`，不查 dirty。
- Codex P0、Grok P1、Agy C5。主控复核成立。升 P0：按标题实现会缩窄 ready set，破 A1。

Decision:

- P5 改名为「交付门 decay **投影**（A1）」。
- merge = `_valid_review_acceptance` + 可选 signoff + `_prepare_merge`。
- 下游 = 只 `_assert_dependency_integrated`。禁止 `if proof.status != live: block downstream`。
- `merge_task` / `check_dispatchable` **不**读 Proof store。
- `explain_task` 对每个 `done` 依赖复用同一祖先检查（修 0.6 已有裂口；`graph.py:181-186` 只看 `done`）。

Acceptance:

- `done` + `review.md` 被撕 / 任务 HEAD 已漂，但 `task-heads.json` 仍是线祖先 → 下游仍 ready。
- 仅 ancestor 失败 → 只报 `TASK_INTEGRATION_PENDING`；`explain.dispatchable=false` 且文案与 `check_dispatchable` 同类。
- `0.6` merge/下游 accept/reject 集合不变。

### P0-F4: 删除「A2 已否决 / 0.7 不拒绝任务仓 dirty」

Evidence:

- 设计文首 A1、§4.2:206、计划 `:158,:271`、ADR 决策 9 均写「不加严任务仓 dirty」。
- 源码 `_collect_task_heads`（`tasks.py:905-909`）在 porcelain dirty 时抛错；local `_valid_review_acceptance` 与 `_prepare_merge` 都走到这里。
- Codex P0、Agy S6。**Cursor 独立段「dirty + HEAD 未变时 merge 仍通过」被源码证伪，本仲裁作废该事实，不改写 Cursor 原文。**
- A1 **核心**（与 0.6 同真值）优先于骑手。骑手建立在错误源码阅读上。

Decision:

- 不重开 A1 核心，不放松 0.6。
- 删除 A2 否决句与「0.7 不拒绝任务仓 dirty」。
- 写明：0.6 **已经**拒绝任务仓 dirty；0.7 保持；Proof 只投影，不得改为 accept，也不得再叠第二道门。
- 定律 II 的「脏」限定为 **merge / review-acceptance 路径**（任务仓 dirty + 开发线 dirty），**不得**延伸到下游。

Acceptance:

- 锁夹具：`done` + 任务仓 dirty + HEAD 未变 → merge reject，错误集与 0.6 相同。
- P2 夹具分表：`review_verdict` ← `_valid_review_acceptance`；线 dirty ← `_prepare_merge`。禁止「同真值」与「dirty 不拒绝」互斥验收并存。

### P0-F5: P4 改挂 `SchedulerSnapshot` / `ProgressFacts`；journal 不存 proofs

Evidence:

- 计划 `:177` 点名 `ContinuationSnapshot`。该类型无任何 `ContinuationSnapshot(` 实例化；生产采样是 `SchedulerSnapshot`（`snapshot.py:53-74`）。
- `decide_no_progress` 只出现在 `tests/test_continuation_budgets.py`；生产 `_budget_usage` 不设 `no_progress_cycles`。
- Agy C1/D4 P0；Codex P1。升 P0：改错类型则 P4 空转或打爆 digest。

Decision:

- P4 目标：`SchedulerSnapshot._payload` + `ProgressFacts` 装配点 + `ReasonCode.PROOF_DECAYED`。
- 标注或废弃计划中的 `ContinuationSnapshot`。
- journal **不**持久化 proofs 当 PASS；`schema_version` 保持 1，新字段缺省空。
- 0.7 **不**把 Proof 接入生产 `BudgetUsage` / no-progress 耗尽。
- 同步 `attention.py` 与 `_schedule_block_reason`；默认 **不**用该码 block 下游。

Acceptance:

- 同一 substrate 下 `build_scheduler_snapshot` digest 稳定；旧 journal 无 proof 字段兼容。
- `test_continuation_budgets` 仍绿；0.7 不新增 no-progress 自动耗尽。

### P0-F6: 发布 derive 规格后再写 `proof/` 代码

Evidence:

- `integration_heads` 无持久文件，由 `git merge-base --is-ancestor` 即时判定（`tasks.py:973-986`）。
- `gate_log` 本地 `gate-{n}.log` vs 外部 `gates.json` + `gates/*.log`。
- `action_receipt` 在 Objective `action-receipts/`，不在 task 目录。
- `examples/polyrepo` 仅有 `dyro.toml`。Agy S2/S3/S4/S5。

Decision:

- P1 附录写清五种 kind 的源路径、id 公式、substrate、`produced_at`（只取记录内字段，禁止 mtime 进身份哈希）。
- `proof list --task` **不含** `action_receipt`。
- 黄金哈希夹具用 `tests/` 现有 bound review，polyrepo 降为 smoke。

Acceptance:

- 表驱动：`integration_heads` 三态与 scheduler 一致；local/external 各一条 `gate_log`；两次派生、不同 mtime，身份哈希相同。

## 4. P1 / P2

**P1（文档修订时一并改，不挡「先改合同」但挡对应切片开工）：**

1. `proof export` 锁定一种 id 语义（proof-id 或 `--task`）；设计 `:188`/`:340` 同形。
2. 0.7 vs 1.0：P3 = `list/show/verify`；P6 `export` 可进 0.7 且标 experimental；`verify-bundle` 硬门禁 + `schema_version = 1` 归 1.0/P13。
3. P7 命令改为 `dyro objective attention`；去掉 phantom `dyro attention`。
4. 定律 I / CLI：默认 `verify` 的 `live` = rebind holds，不是 procedure 已复现；未 `--rerun-procedure` 的 `gate_log` 不得 `procedure_reproduced=true`。
5. Proof 签名槽：增加 `declared_key_ids` + 导出时 `require_signed_*` 快照，**或**删除「缺已声明密钥 → inconclusive」并禁止与控制面同结论。二选一。
6. P6 拒绝 `task evidence` ZIP 布局；不把 git 对象放入 bundle。
7. 定律 III：review adapter 不是开发线/任务源码写入者，但必须能写 `review.md`。
8. P11：默认 `.dyro/host-projections/`；`tools.json` / PATH = `discovered_unintegrated`；`--user` 才写用户级 skill。

**P2：**

1. `require_clean_merge` 写明为 schema 不变量，线脏预检硬编码。
2. P0 文档切片把不变量 14–20 回写 `architecture.md`。
3. 0.8 前补操作格与已有 mutation（`claim` / `evidence import` / `answer` / `changeset`）映射。

## 5. Open Micro-Decisions

| # | 决议 | 依据 |
| --- | --- | --- |
| 1. `proof verify` 默认 | **锁定 decay + rebind，不重跑 gate。** `--rerun-procedure` 仅诊断，须 dry-run/隔离。 | 四人一致；`merge_task` 无 `run_gates` |
| 2. P7 是否进 0.7 | **滑到 0.8。** 0.7 出口改为 P1–P5 + P3 CLI；P6 export 可选、experimental。若产品坚持 0.7 做 Console：summary **零新 git I/O**，衰减走 `not_inspected` 或独立 inspect。 | Cursor/Grok/Codex 可滑；Agy 建议留下。A1 不依赖 Console；先滑以降低假 `live` 广播面。 |
| 3. 宿主投影默认工作区 | **锁定。** 与 `agent`/`tool` 发现面不固有冲突。禁止把 `tools.json`/PATH 当 Card。 | 四人一致 |

## 6. Instructions For The Execution Agent

```text
First revise the spec/plan. Do not implement business code yet.

Read:
/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev_0814/dyroengineeringflow/docs/superpowers/reviews/2026-08-15-delivery-physics-adversarial-review-board.md
Focus on: Final Arbitration P0-F1 … P0-F6.

Must close in:
- docs/designs/delivery-physics.md
- docs/adr/0006-delivery-physics-and-capability-plane.md
- plans/delivery-physics-implementation.md
- docs/architecture.md（不变量 14–20 可与 P0 文档切片同 PR）

Do not:
- edit other reviewers' original sections
- start src/dyro/proof/ or change merge_task / check_dispatchable
- leave P0 as "implementation note"
- reopen A1 core / B1 / 投影 B / 品类
- treat Cursor 独立段关于「dirty merge 仍通过」为事实

Write back:
- files and sections changed
- status for each P0: closed / open / needs user decision
- updated Go/No-Go
```

## 7. Conditions To Start Implementation

关闭 P0-F1…F6 的文档 diff 后：

- **可开：** P1 derive + 测试夹具（非 polyrepo 黄金哈希）、P3 CLI。
- **仍不可开：** P2/P5 实现（须合同已改且夹具不再互斥）；P4（须类型映射已改）。
- **0.7 身份：** P1–P5 + P3；P6 export 可选；P7 默认不进。

## 8. Requires Human Verification

- ~~Proof `contract_hash` 用哪一个。~~ **已锁定（2026-08-15 用户确认）**：task 面 → attempt `task_contract_sha256`；`action_receipt` → Objective `contract_sha256`。
- ~~`proof list` / `verify` 是否每次重派生。~~ **已锁定**：每次全量重派生；store 不是 PASS。
- 若产品坚持「0.7 就要在 Console 看见 Proof」，覆盖微决策 2，但仍须 C01（summary 零新 git I/O）。
- 若用户本意是**放松** 0.6 的任务仓 dirty 拒绝：须显式改口；本仲裁按 A1 核心「同真值」解释为保持拒绝。

Final signature: Cursor（主控）
四人签名已齐。Cursor 独立段填写时未见另外三份；本仲裁是唯一合并意见。

---

## 9. Execution writeback（2026-08-15）

文档契约已按本仲裁修订。未写 `src/dyro/proof/`，未改 `merge_task` / `check_dispatchable`。

| P0 | 状态 |
| --- | --- |
| P0-F1 全量 decay 谓词 | **closed**（设计 §4.2 表 + 定律 II + ADR 决策 9 + 计划 P2） |
| P0-F2 verify / verify-bundle 分家 | **closed**（设计 §4.1 / §11 + ADR 决策 10 / 后果 + 计划 P6/P13） |
| P0-F3 P5 下游只投影祖先 | **closed**（计划 P5 更名与验收；设计定律 II / §4.2） |
| P0-F4 删除 A2 dirty 假描述 | **closed**（三份文首 + 计划延后表；保持 0.6 拒绝） |
| P0-F5 P4 改挂 SchedulerSnapshot | **closed**（设计 §4 / §6 + 计划 P4） |
| P0-F6 derive 规格附录 | **closed**（计划附录 A；设计 §4.1 指向该附录） |

P1 文档项（export id、列车、`dyro objective attention`、rebind `live`、签名槽、evidence ZIP、定律 III、P11 scope）已写入设计/计划。P2：`require_clean_merge` 不变量已写；架构不变量 14–20 已回写 `architecture.md`。操作格与已有 mutation 映射仍待 0.8。

更新后 Go/No-Go：文档 **Go**。P1 derive + P3 CLI **可开**。P2/P4/P5 **实现仍 No-Go，直到对应 PR 按已改合同写夹具**（合同本身已改）。

须人工核已关（用户确认按推荐）：`contract_hash` 按 subject 拆；`list`/`verify` 每次重派生。仍开：P7 是否被产品改回 0.7；是否放松任务仓 dirty（默认不放松）。
