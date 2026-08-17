# Dyro 交付物理学实施计划

状态：待批准；2026-08-15 锁定 A1 / B1；同日对抗评审仲裁已收口契约；2026-08-16 版本列车收口为 `0.7.x`  
设计：[`docs/designs/delivery-physics.md`](../docs/designs/delivery-physics.md)  
ADR：[`docs/adr/0006-delivery-physics-and-capability-plane.md`](../docs/adr/0006-delivery-physics-and-capability-plane.md)  
仲裁：[`docs/superpowers/reviews/2026-08-15-delivery-physics-adversarial-review-board.md`](../docs/superpowers/reviews/2026-08-15-delivery-physics-adversarial-review-board.md)  
基线：`0.6.0` 已发布的 TaskGraph、证据绑定、Objective、只读 Console；`0.7.0` 已发布 Proof / Card / Compiler / `verify-bundle`  
默认策略：先抽出投影，再衰减进调度，再换 Card，最后编译宿主；每一阶段未绿之前，下一阶段保持关闭。功能和发版继续往前，版本号保持 `0.7.x`，不另开 `0.8.0` / `0.9.0` / `1.0.0` 功能号。

已锁定：

- **A1**：`0.7` 的 merge / 下游对错与 `0.6.0` 相同。Proof 只投影现有绑定检查，只多 `PROOF_DECAYED`。`merge_task` / `check_dispatchable` 不读 Proof store。
- **B1**：`verify-bundle` = Proof Bundle + 调用方 git 对象。核验完整性，不核验身份，不承诺与当前工作区 `verify` / merge 同一套 `live` / `decayed`。捆内不塞对象库。
- 任务仓 dirty：`0.6` 已拒绝（`_collect_task_heads`），`0.7` 保持。不是「已否决的新规则」。
- `ContinuationSnapshot` 是死类型。P4 只改 `SchedulerSnapshot` / `ProgressFacts`。

已锁定微决策：

| # | 决议 |
| --- | --- |
| 1 | `proof verify` 默认 decay + rebind，不重跑 gate。`--rerun-procedure` 仅诊断，须 dry-run/隔离。 |
| 2 | Console P7 留在 `0.7.x`，不另开 `0.8`。`0.7.0` 已发 P1–P6 + P8–P12a。P7 / `trigger_observation` / P13 走后续 `0.7.x`。 |
| 6 | 功能和发版继续往前，版本号保持 `0.7.x`。不另开 `0.8.0` / `0.9.0` / `1.0.0` 功能号。`1.0.0` 只是以后的身份冻结，不是下一列功能车。 |
| 3 | 宿主投影默认当前工作区。`tools.json` / PATH = `discovered_unintegrated`。`--user` 才写用户级 skill。 |
| 4 | `contract_hash` 按 subject 拆：task 面 kind → attempt `task_contract_sha256`（缺则空）；`action_receipt` → Objective `contract_sha256`。 |
| 5 | `proof list` / `verify` 每次全量重派生。store 可丢弃，不是展示真源。 |

---

## 1. 最终交付结果

完成本计划后，Dyro `0.7.x` 对外可证明：

1. 已有 receipt / review / heads / signoff / action receipt 可被列为 Proof，且不复制真源；
2. `dyro proof verify` 对**当前工作区**做衰减与绑定重算（rebind，不是 replay）；`verify-bundle` 用 bundle + 调用方 git 对象做**完整性**复验，两套结论不得混称；
3. `0.7` 衰减与现有 merge / 下游检查同真值；只把已发生的拒绝用 `PROOF_DECAYED` 说清楚，不另造拒绝条件，不加严下游 ready set；
4. Capability Card 取代「只有 argv 的 adapter」，旧 Profile 仍能加载；
5. Host Compiler 只把本机已审计能力投影为宿主 `SKILL.md`；过期投影阻断自动 mutation；
6. README 与术语扫描把产品身份锁在 Delivery Physics，而不是 agent 编排；
7. Console / Home 在 `0.7.x` 用独立 inspect 显示已投影的 Proof 状态与衰减原因，仍无写权。summary 保持 `proof_inspection=not_inspected`，与 `dyro objective attention` 不是同一套 Proof 展示。

---

## 2. 实施原则

- 每个 PR 只做一个可回滚切片；依赖未合并前，后续分支不重写其代码。
- 先写失败测试，再写最小实现，再跑全量回归。
- 新增 mutation 必须有 dry-run、锁、ledger、失败恢复。
- 不直接改写 Task 质量门状态；`0.7` 衰减只投影现有受保护 API 的拒绝，不增加新的 merge / 下游拒绝条件。
- 不把设计文档里的否决项当成「以后再说的优化」。
- 权威投影已锁定为 B：P11 先交 skill；P12a 只对已证明 hook 表面的宿主投影 deny hook。禁止因无 hook 拒绝 compile。
- 不修改用户当前无关 checkout；不把业务仓库名写进 Core 测试以外的夹具。
- 未关闭仲裁 P0 文档项之前，不得合并 P2 / P4 / P5 实现。

---

## 3. 版本列车

| 版本 | 主题 | 对用户可见 | 关闭条件未满足时 |
| --- | --- | --- | --- |
| `0.6.x` | 已发布维护 | 只接受与本计划不冲突的修复 | 不回写 `0.7` 物理列车 |
| `0.7.0` | 已发布：Proof / Card / Compiler / `verify-bundle` | `dyro proof list/show/verify`；`export`；`verify-bundle`；`dyro capability *`；`dyro host compile`；`dyro objective attention` 可含 `PROOF_DECAYED` | 已关闭。不得改号为 `0.6.x` 或 `1.0.0` |
| `0.7.x` | 后续全部功能与发版 | 已发 P7 / `trigger_observation` / P13；后续产品面与新能力继续打 `0.7.2`、`0.7.3`…… | 不另开 `0.8.0` / `0.9.0` / `1.0.0` 功能号 |
| `1.0.0` | 身份冻结，不是本系列功能号 | 仅当产品显式要求时才打 | 未显式要求不得标 `1.0.0` |

`0.6.x` 继续只接受维护修复。本计划的剩余实现继续走 `0.7.x`，不回写 `0.6.0` 的已发布语义，也不把同一批功能改标成 `0.8` / `0.9` / `1.0`。

---

## 4. 模块地图

```text
src/dyro/proof/
  models.py        Proof、DecayDecision、BundleManifest
  derive.py        从 receipt/review/heads/signoff/action 派生存活对象（见附录 A）
  decay.py         纯函数 decay(proof, substrate, clock)
  evaluate.py      默认衰减与绑定重算（工作区 rebind）
  project.py       Proof 展示投影；list/verify 默认重派生，无 store
  bundle.py        导出/导入 ZIP；调用方 git 对象做完整性核验；拒绝 evidence ZIP 布局

src/dyro/capability/
  models.py        CapabilityCard、IsolationClass、Intent
  cards.py         adapters.* → Card 的只读升级
  store.py         已审计 Card 写入
  probe.py         doctor 可执行探测；PATH 发现不进入 execute
  cli.py           capability list/add/test

src/dyro/host/
  compile.py       输入 Cards + 探测 → 投影树与 SKILL.md
  doctor.py        重算哈希；手改 fail-closed
  models.py        投影 manifest / authority 标签

现有模块保持职责：
  tasks.py / reviews.py / evidence*.py / provenance.py   真源
  continuation/snapshot.py + budgets.py                  SchedulerSnapshot / ProgressFacts
  continuation/models.py                                 ReasonCode.PROOF_DECAYED；ContinuationSnapshot 保持死类型
  continuation/attention.py                              PROOF_DECAYED → AttentionKind.NEEDS_USER
  console/read_model.py                                  0.7.x 独立 inspect 只读展示；summary 零新 git I/O
  tooling.py                                             发现结果供给 Compiler；不得当 Card
  profile.py / config.py                                 加载旧 adapters
```

禁止把 Proof 缓存写成可以绕过 review 绑定的第二份 PASS。

---

## 5. PR 依赖图

```text
P0  文档与术语冻结
 │
P1  Proof 模型与从现有文件派生
 │
P2  Decay 纯函数 + 单测夹具
 │
P3  proof CLI（list/show/verify）
 │
P4  SchedulerSnapshot 纳入 Proof 投影；reason code PROOF_DECAYED
 │
P5  交付门 decay 投影（A1）
 │
P6  Proof Bundle export（0.7 experimental）
 │
P7  Console/Home 独立 inspect 只读展示（0.7.x）
 │
P8  Capability 模型 + adapters 迁移
 │
P9  capability CLI + attest/doctor
 │
P10 未审计命令保持发现-only（含 OpenCode 探测，不给 execute）
 │
P11 Host Compiler 核心 + SKILL.md
 │
P12 host doctor + 过期投影阻断自动 mutation
 │
P12a 可选 deny hook（仅已证明 hook 表面的宿主）
 │
P13 0.7.x 叙事、schema 冻结、verify-bundle 可作为后续 0.7.x 门禁（仍不打 1.0.0）
```

并行允许：

- P2 可在 P1 模型冻结后与 P3 的 CLI 骨架并行，但 P3 的 verify 必须等 P2。
- P7 不得早于 P2/P5 同真值落地；走 `0.7.x`，不得等待 P6，也不得另开 `0.8`。
- P8 不得早于 P5：先保证旧 adapter 世界里衰减已经生效。
- P11 不得早于 P9。P12a 不得早于 P12；无 hook 宿主上 P12a 必须仍使 compile 成功。
- P6 `verify-bundle` 实现可与 P6 export 同文件。`0.7.0` tag **不**以其为硬门禁；后续 `0.7.x` 才可把它加成发布门，且仍不因此打 `1.0.0`。

---

## 6. 分 PR 说明

### P0 · 文档与术语冻结

- 合并本设计、ADR-0006、本计划（含本仲裁修订）。
- 术语扫描只禁精确短语：`multi-agent platform`、`skill marketplace`、`open-source alternative`。现有 `multi-agent dispatch` 不在禁列。
- 策略文件必须在仓库外（现有 `dyro terminology check` 约束）；CI 用环境或外部文件，不把对照表写进树。
- 允许词：`delivery control plane`、`delivery physics`、`proof`、`capability card`。
- 架构不变量 14–20 回写 `architecture.md`，避免设计/架构双清单。
- 验收：文档进树；外部策略扫描绿；无代码行为变化。

### P1 · Proof 派生

- 只读扫描现有任务目录，派生 `gate_log` / `review_verdict` / `signoff` / `integration_heads` / `action_receipt`。这是 `0.7` 的 kind 闭集。算法见**附录 A**。
- `0.7.0` 未派生 `trigger_observation`。后续 `0.7.x` 已从 `objectives/<id>/triggers/<trigger-id>.json` 派生该 kind，只用 `next_probe_at`，不另开 `0.8`。不把 `external_bundle` 当成新 ZIP；后者若出现，只是已有 evidence ZIP 世代的投影。
- 不改写 `review.md`、receipt、ledger。**不**把 ledger 当 gate PASS。
- `produced_at` 只取记录内字段；`generation` 用证据世代或 attempt 世代。身份哈希不含「现在」、mtime、`produced_at`。
- 缺绑定字段 → `inconclusive`，不伪造 live。
- `proof list --task` **不含** `action_receipt`。
- 验收：必须引用 `tests/test_tasks.py` 等现有 evidence / bound-review 夹具 + 新建 `tests/test_proof_derive.py`。`examples/polyrepo` 仅 smoke（`proof list` 不 crash），**不是**黄金哈希源。两次派生、不同 mtime，身份哈希相同。

### P2 · Decay

- 纯函数，禁止读时钟以外的全局状态；时钟由调用方注入。
- **分表**，禁止混为一表：
  - `review_verdict` ← `_valid_review_acceptance`（receipt / `task-heads.json` / attempt / plan / local `_assert_task_heads_current`，含任务仓 dirty）。
  - `signoff` ← `_valid_external_signoff`。
  - 开发线 dirty / 错分支 ← `_prepare_merge`；现有 merge 错，**不是** `PROOF_DECAYED`。
  - 下游祖先 ← `_assert_dependency_integrated`。
- 不把 `git revert` 当成祖先断裂。
- 任务仓 dirty + HEAD 未变：merge **拒绝**，错误集与 0.6 相同。补锁夹具。
- `gate_log` 内容哈希变化可以展示为 decayed；`0.7` merge 不因这条新拒绝。
- 验收：表驱动测试命名对应源码函数；无 I/O；与现有谓词同真值的夹具必须绿。不得同时要求「dirty 不拒绝」。

### P3 · `dyro proof` CLI

```text
dyro proof list [--task ID] [--objective ID] [--line ID]
dyro proof show <proof-id>
dyro proof verify <proof-id> [--dry-run]
```

- JSON 与人话共用同一 projection。`live` = 当前 substrate 上 rebind 成立，不是 procedure 已复现。
- `verify` 默认做衰减与绑定重算，不重跑 gate argv，不改 Task 状态机，无 gate 子进程。
- `--rerun-procedure` 才重跑，且必须 dry-run 或隔离；ledger 只记 rerun 或状态翻转。未 replay 的 `gate_log` JSON 不得出现 `procedure_reproduced=true`。
- 验收：默认 verify 无 gate 副作用；dry-run 无写；失败退出码区分 decayed / inconclusive / error。

### P4 · 续航快照

- 目标类型是 **`SchedulerSnapshot._payload`** 与 **`ProgressFacts` 装配点**（supervision / planner 交界）。**禁止**给未实例化的 `ContinuationSnapshot` 加 `proofs[]`。
- journal **不**持久化 proofs 当 PASS。`SchedulerReadProjection.schema_version` 保持 `1`；新字段缺省空，不进 merge 真源。
- planner / `ReasonCode` / `attention.py` / `_schedule_block_reason` 同步加 `PROOF_DECAYED`。attention 映射 `AttentionKind.NEEDS_USER`。默认 **不**用该码 block 下游。
- `progress_fingerprint` 继续忽略 trigger 类 Proof。**不**把 Proof 接入生产 `BudgetUsage`，不新开 no-progress 自动耗尽。`trusted_usage` 接入 `BudgetUsage.provider_usage_trusted` / `BudgetRequest.provider_usage_trusted`，默认 `false`。`objective tick` 对 automatic Objective 预览 `decide_budget(..., automatic=True)`；受监督 apply 保持 `automatic=False`。未信任硬停要求显式 `workspace.max_provider_usage`。文档承认：`decide_no_progress` 是已锁纯函数，生产未接线。
- 验收：同一 substrate 下 `build_scheduler_snapshot` digest 稳定；旧 journal 无 proof 字段兼容；`test_continuation_budgets` 仍绿；衰减后下一 tick 可出现 attention，不自动重跑 agent。

### P5 · 交付门 decay 投影（A1）

- `task merge` 仍只走 `_valid_review_acceptance` + 可选 `_valid_external_signoff` + `_prepare_merge`。
- 下游释放仍只走 `_assert_dependency_integrated`。禁止 `if proof.status != live: block downstream`，禁止改 `build_task_readiness` 的接受集合。
- Proof 只给这些**已经发生**的检查一个 `live` / `decayed` 名字。`PROOF_DECAYED` 仅用于 merge 人话 / attention。
- `task explain` 对每个 `done` 依赖复用同一祖先检查（`_assert_dependency_integrated` 或 snapshot `integration_state`），**不**读 Proof 缓存。这是修 0.6 已有裂口，不是加严。
- 验收首条：`_valid_review_acceptance` / `_assert_dependency_integrated` 的 accept/reject 集合与 0.6 **bitwise 相同**。夹具「`done` + `review.md` 被撕 / 任务仓 HEAD 已漂，但 `task-heads.json` 仍是线祖先」→ 下游仍 ready；仅 ancestor 失败 → 只报 `TASK_INTEGRATION_PENDING`；`explain.dispatchable=false` 且文案与 `check_dispatchable` 同类。diff 中 `merge_task` / `check_dispatchable` 不新增 Proof store 读取。

### P6 · Proof Bundle（B1：完整性，不是身份）

- ZIP：manifest、被核验字节、procedure 描述、钉死的 heads/hashes、`declared_key_ids`、`policy_require_signed` 快照。
- 剥离路径、凭据、prompt、adapter env。**不**放入 git 对象库。
- CLI：

```text
dyro proof export <proof-id> --bundle PATH
dyro proof export --task ID --bundle PATH
```

  位置参数是 proof-id；`--task` 批量导出。二者互斥。help 与设计同形。
- 拒绝 `task evidence build` 的 ZIP 布局：对其跑 `verify-bundle` → `inconclusive`，不是 `live`。
- `verify-bundle` 必须由调用方提供 git 对象（`--git-dir` 或测试夹具里的 bare repo）。无 `--current-heads` 不得报与 merge 相同的衰减结论。
- 缺 procedure、缺 substrate、缺 git 对象、或缺已声明的签名密钥 → `inconclusive`，不得 `live`。
- **列车：** `export` 已进 `0.7.0`，标 experimental。`verify-bundle` 硬门禁与叙事锁是后续 `0.7.x` / P13，不另开 `1.0.0` 功能号。
- 验收：单任务多 proof 导出有表驱动测试；干净环境带固定 git 夹具得到与源机相同的**完整性**结论（不是「现在能否 merge」）；不提供 git 对象时为 `inconclusive`；机密扫描零命中。

### P7 · 只读展示（`0.7.x` 独立 inspect）

- `dyro objective attention <id>` 与 Console **独立 inspect** 显示 Proof 状态与稳定 reason。**无**顶层 `dyro attention`。
- 不展示 argv、绝对路径、日志正文。
- `capture_workspace_read_snapshot` / summary **零新 git I/O**，保持 `proof_inspection=not_inspected`。衰减展示只走独立 inspect，不得把 summary 写成与 `objective attention` 同一套 Proof 入口。打破 `test_console_read_model.py` 的「summary 不探 Git」即为回归。
- 验收：既有 Console 只读攻击夹具仍绿；浏览器无新写入口；独立 inspect 可展示已投影 Proof；`dyro objective attention` JSON 在 merge 相关 decay 时可含 `PROOF_DECAYED`。

### P8 · Capability 迁移

- `Config.adapters` 仍可解析。
- 运行时升级为 Card；缺省 isolation=`cwd`，`cannot_prove+=done,merge`。
- `dyro.toml` 可开始写 `[[capabilities]]`；两者共存时 ID 冲突 fail-closed。
- 验收：旧 examples 零改动仍能 `doctor`；新 schema 有正反解析测试。

### P9 · capability CLI

```text
dyro capability list
dyro capability add <id> --preset ...
dyro capability test <id>
```

- `test` 做登录/可执行探测，不启动交付。hook 表面若存在，写入同一份报告字段，不另造 `--host-id`。
- `agent add` 仍写 `[adapters.*]`；运行时升级为 Card。写 `[[capabilities]]` 用 `capability add`。
- 验收：`noop` / `codex` preset 行为与 0.6 兼容。

### P10 · 发现但不执行

- 探测 `opencode`、`cursor-agent` 等，标记 `discovered_unintegrated`。
- Objective / `task run` 不得因探测成功而选中它们。
- `dyro tool list` / `tool install` / `tool default` / `dyro open` 不得写入可执行 Card，也不得被 Objective 选中。`0.7.x` Card 只包 adapters。
- 验收：PATH 里有假 `opencode` 可执行文件时，自动执行仍 fail-closed。

### P11 · Host Compiler

- 渲染 Agent Skills `SKILL.md`：定律摘要、本机可用表、负例、只打印 `dyro next` 命令。
- 输入：`config.adapters` + `capability test`。**不**把 `registry_home()/tools.json` 或 PATH 发现当已审计 Card。
- 默认只写**当前工作区** `.dyro/host-projections/`。用户级目录（Codex home skills 等）必须显式 `--user`，doctor 标 `scope=workspace|user`。路径来自探测，不写死客户名。
- 原子替换 + 投影清单哈希。
- 验收：无可用 Card 时产物不含任何 execute 暗示；有 Card 消失后重编译删除对应段落；doctor 报告含 `scope`。

### P12 · 投影医生

- `dyro host doctor` 重算哈希。
- 过期或手改 → 非零退出；若存在 ActivationLease，下一 mutation tick fail-closed 为 plan-only。
- 验收：改一个字节的 SKILL.md 即失败；修复后恢复。

### P12a · 可选 deny hook

- 仅当宿主 Card 声明并被 `capability test` 证明存在 hook 表面时，编译 deny hook。
- Deny 从操作格生成：未授权的 `integrate` / `publish`，以及写入 `.dyro/`。
- 无 hook 宿主：compile 成功，`authority_projection=skill_only`，doctor 不失败。
- 已投影 hook 被删或哈希漂移：doctor fail-closed。
- 文档与 CLI 帮助不得把 hook 写成沙箱或隔离。
- 验收：假 hook 表面不得触发 hook 文件；无 hook 的 OpenCode 夹具仍能 compile。

### P13 · `0.7.x` 可携带核验与叙事锁

- Bundle schema 锁 `schema_version = 1`。
- 发布工件含「陌生人核验」CI：从 sdist 安装的干净环境，用夹具 git 对象跑 `verify-bundle`，断言**完整性**结论，不断言与源机当前 HEAD 的 merge 对错相同。
- README 各语言同步身份句，术语扫描覆盖翻译文件。
- 验收：缺 P5/P6-export/P12 任一证据，后续 `0.7.x` 发布工作流可以拒绝打新 tag。这是 `0.7.x` 门禁，不是改打 `1.0.0` 的理由。未显式要求不得标 `1.0.0`。

---

## 7. 明确延后（不是本列车）

| 项 | 延后原因 |
| --- | --- |
| OpenCode / Cursor 的经审计 execute adapter | 需要独立协议审计，不属于物理学抽出 |
| 把 hook 做成所有宿主的强制门槛 | 已否决（选项 C）；P12a 保持可选 |
| CI / Linear Trigger provider | 已有 Trigger 扩展点；观察不得完成任务 |
| HMAC 审计链 | Witness 已有哈希链；重复造链没有产品增量 |
| 自动 push / 发布 | 仍显式；不因 `0.7.x` 收口而自动发布 |
| Skill 投影评测 | 先有稳定投影，再谈评测 |
| 沙箱 backend entry point | Card 先能声明 isolation，再插拔实现 |
| 检测 revert 是否撤掉了变更 | 不是祖先问题；另立规则后再做 |
| Bundle 自含 git 对象库 | B2，已否决；调用方提供对象 |
| 生产接线 `decide_no_progress` | 0.7 只保证纯函数契约 |
| 放松任务仓 dirty 拒绝 | 会改 0.6 对错；除非产品显式改口 |

---

## 8. 风险与对策

| 风险 | 对策 |
| --- | --- |
| Proof 缓存被当成第二份 PASS | 缓存可全量重建；list/verify 默认重派生；merge 仍读原始绑定字段 |
| 用户以为 `host compile` 等于授权 merge | 产物只含 observe + 打印命令；文档与负例双写 |
| Card 迁移弄坏旧 Profile | 只读升级；冲突 fail-closed；examples 零改动测试 |
| 衰减导致「合法工作无法合并」 | A1：0.7 对错与现在相同；人话指向现有修复命令 |
| 把 revert 当成祖先断裂 | 不实现；祖先检查只问提交是否仍在历史上 |
| 以为 bundle 自带 git 对象 | B1：调用方提供对象；缺对象 → inconclusive |
| 以为 `verify-bundle live` = 现在能 merge | 两条命令两套结论；无 `--current-heads` 不报衰减 |
| 改错快照类型 / 写入 journal | P4 只碰 `SchedulerSnapshot`；journal 不存 proofs |

---

## 9. 阶段出口

**0.7.0 已发布：** P1–P6 + P8–P12a 绿。旧工作区不改 toml 即可 `proof list`；merge / 下游对错与 0.6 相同，merge 错误路径只多 `PROOF_DECAYED` 人话。`0.7.0` tag 检查**不含** `verify-bundle` 硬门禁。

**0.7.x 继续：** P7 / `trigger_observation` / P13 已在 `0.7.1` 落地。后续功能与产品面收口继续开发和上线，号写成 `0.7.2`、`0.7.3`……，不另开 `0.8.0` / `0.9.0` / `1.0.0`。

**1.0.0：** 不是本系列功能出口。未显式要求不得标 `1.0.0`。

任一出口的「绿」指：单测、现有 unittest 全量、ruff 基线、术语扫描、以及该阶段新增的 fail-closed 夹具。

---

## 附录 A · 0.7 Proof derive 规格

身份：`id = sha256(kind || subject || generation || identity_payload)`。`identity_payload` **不含** `produced_at`、mtime、`now`。`list` / `verify` 默认全量重派生；没有 Proof store。

`contract_hash`（已锁定）：task 面 kind 用 attempt `task_contract_sha256`（缺则空，不伪造）；`action_receipt` 用 Objective `contract_sha256`。`list` / `verify` 每次全量重派生；store 不得当展示真源。

| kind | 源路径 | subject | substrate | produced_at | generation / 何时物化 | decay |
| --- | --- | --- | --- | --- | --- | --- |
| `gate_log`（local） | `{task.directory}/logs/gate-{n}.log`（`_capture` 真源）+ `receipt.md`。argv 哈希取自 `task.toml` 的 gate 定义，**不**读 ledger | `task_id` | 被测树 heads + gate argv 哈希 + contract_hash | 空（日志无记录内时间） | 证据世代或 attempt。derive 时扫描 `logs/` 与任务根 | argv 或树内容哈希变 → **展示** `decayed`；merge 不新拒绝 |
| `gate_log`（external） | 当前 evidence generation：`gates.json` + `gates/gate-{n}.log`（`evidence.py` / `tasks.py` 导入路径） | `task_id` | 同上 | 空，除非记录内已有时间字段 | 同上 | 同上 |
| `review_verdict` | `review.md` + `receipt.md` + `task-heads.json` + attempt/plan 绑定 | `task_id` | receipt SHA、`task-heads.json` SHA、`attempt_id`、`plan_sha256`、local 当前 heads | 空（`review.md` 无时间字段）。signed review JSON 的 `created_at` 仅可展示，不进身份哈希 | 绑定的 attempt | `_valid_review_acceptance` 全量 |
| `signoff` | `signoff.json` | `task_id` | `review_sha256`、receipt、heads、attempt、plan | `signed_at` | 同 attempt | `_valid_external_signoff` 全量 |
| `integration_heads` | **无持久文件**。即时 `git merge-base --is-ancestor <task_head> HEAD`，与 `_assert_dependency_integrated` 同一调用 | 被检查的依赖 `task_id`。列下游任务时按 `depends_on` 展开 | 当前线 HEADs + 依赖 `task-heads.json` | 空 | derive / verify 时物化，不写盘当真源。缺 git → `inconclusive` | 祖先成立 → `live`；reset / 换历史 → `decayed`；`git revert` 不衰减。三态与 scheduler `integration_state` 一致 |
| `action_receipt` | Objective 目录 `action-receipts/`（`action_journal.py`），**不是** task 目录 | `objective_id` | intent / authority / budget 字段 | `created_at` | journal 世代 | 字段或世代被替换 → `decayed`。`proof list --task` **不返回**；`--objective` 或后续 `0.7.x` 再暴露 CLI |

缺文件、缺工具、不可解析 → `inconclusive`，不得 `live`。
