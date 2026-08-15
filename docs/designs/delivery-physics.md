# Dyro 交付物理学与能力平面

状态：提案；2026-08-15 锁定 A1 / B1  
目标版本：`0.7.0` 起分阶段落地，`1.0.0` 收口产品身份  
适用范围：Dyro Core、Profile、Host Compiler、Witness；不改写已发布的 TaskGraph / Objective / Console 权威语义

已锁定：

- **A1**：`0.7` 衰减是现有 merge / 下游绑定检查的投影。接受与拒绝与 `0.6.0` **同真值**，只多 `PROOF_DECAYED`。任务仓 dirty：`0.6` 已拒绝（`_collect_task_heads`），`0.7` 保持拒绝；不叠第二道 Proof 门，也不放松。不把 `git revert` 当祖先断裂。
- **B1**：`1.0` 的 `verify-bundle` 核验完整性：Proof Bundle + **调用方提供的** git 对象。捆内不塞对象库。核验完整性，不核验身份，**不承诺**与当前工作区 `proof verify` / `task merge` 得到同一套 `live` / `decayed`。缺 procedure、缺 substrate、缺 git 对象、或缺已声明的签名密钥 → `inconclusive`。

关联：

- [架构与 Profile 契约](../architecture.md)
- [ADR-0004 续航引擎](../adr/0004-native-continuation-engine.md)
- [ADR-0006 交付物理学](../adr/0006-delivery-physics-and-capability-plane.md)
- [实施计划](../../plans/delivery-physics-implementation.md)

---

## 0. 一句话

**Dyro 是本地优先的多仓交付物理引擎：决定什么能变成真，真值何时衰减，以及谁有权改世界。**

完成是外部可观察的物理事实，不是对话里的承诺。

---

## 1. 产品判断

### 1.1 我们已经赢在哪一层

到 `0.6.0`，Dyro 已经不是「任务列表 + 启动器」：

- 交付原子是 **开发线**，不是仓库，也不是 issue。
- 任务属于且只属于一条线；下游只在精确 HEAD 进入该线后才被释放。
- Agent 自报不是证据；gate、receipt、review、signoff、merge 是私有流程。
- Objective 在时间上续航，但不复制 Task，不另造 backlog。
- Home / Console 只有导航和注意力，没有交付权。
- dispatch 只产建议；Witness 让删除和改写可被发现。

这些不是功能清单，是一套已经在跑的**物理学**。行业里大多数热门项目还停在「把更多 agent 塞进更多 worktree」。

### 1.2 我们还没有赢的原因

物理学是隐式的。它散落在 `review.md` 绑定、`task-heads.json`、attempt、ledger、以及每次重建的 `SchedulerSnapshot` 里。外人看到的是 CLI，看不到定律。隐式定律无法被引用、无法被第三方复验、无法被编译到宿主上。**第一名属于能把定律说清楚、并让别人按同一套定律核验的人。**

### 1.3 我们拒绝的跟跑形态

| 形态 | 为什么是跟跑 | Dyro 的落点 |
| --- | --- | --- |
| 舰队 UI | 把并行会话当产品。谁的界面更能并排开窗口，谁也没有交付真理。 | Console 只读；并行来自 ready set 与冲突组。 |
| 提示词超市 | 内容过时、膨胀、污染上下文。Core 若变成提示词仓库，就失去机制身份。 | 必经门在 Core；宿主只拿收缩后的投影。 |
| 工单即真源 | 管理工作是对的；把议题跟踪器当成原子是错的。 | 议题若接入，只是 Trigger，不是 Line，也不是 Proof。 |
| 角色剧场 | 拟人角色掩盖「一个冲突组只有一个写入者」。 | 只用 line、task、proof、review、signoff。 |
| 模型编排 | 协调环里放 LLM，计划不可复放。 | 调度是纯函数；协调环无模型。 |
| 第二份计划图 | 另造一份工作流清单，与 TaskGraph 双写漂移。 | `task.toml` 与编译后的 TaskGraph 是唯一交付图。 |
| 粘滞的完成故事 | 视频、摘要、状态字被当成交接物。 | Proof 带核验程序，且会衰减；不确定不得写成通过。 |

---

## 2. 四条定律（架构灵魂）

定律是 Core 不变量。Profile 不能放宽它们。Host Compiler 只能把它们投影得更窄。

### 定律 I · 外部真值

一句话在被独立程序、针对钉死的 substrate 复现之前，不是事实。

- Agent 文本、多模型投票、Trigger 摘要、Console 展示，都是声称。
- 事实必须带 **核验程序**（Core 内置或 Profile argv）和 **substrate 绑定**（HEAD、plan hash、attempt、合约哈希）。
- 不确定就是不确定。禁止把 missing / unparseable / 环境不足升级为通过。
- 默认 `dyro proof verify` 是 **rebind**（衰减 + 绑定重算），不是 replay。它的 `live` 表示当前工作区 substrate 上绑定仍成立，**不是** procedure 已复现。`gate_log` 未加 `--rerun-procedure` 时，不得叙事成「门禁仍通过」或输出 `procedure_reproduced=true`。

### 定律 II · 衰减

真值不粘滞。substrate 一动，事实死亡。状态字符串不得比它的绑定活得更久。

衰减的**展示**与 merge / 下游的**拒绝**不是同一句话。哪条影响哪扇门，必须与 `0.6.0` 源码同构：

- `review_verdict` 在 `_valid_review_acceptance` 为假时失效：receipt SHA、`task-heads.json` SHA、`attempt_id` / `plan_sha256` 绑定、以及 local 下 `_assert_task_heads_current`（含任务仓 porcelain dirty）。这是 **merge / review-acceptance** 路径。
- `signoff` 在 `_valid_external_signoff` 为假时失效（再绑 review / heads / attempt / plan；策略开启时含 Ed25519）。
- 开发线 dirty 与错分支由 `_prepare_merge` 硬编码拒绝。`policy.require_clean_merge` 只能为 true，是 schema 不变量，不是运行时开关。这两类错误**不得**标成 `PROOF_DECAYED`。
- 下游释放只投影 `_assert_dependency_integrated`（`git merge-base --is-ancestor`）。decayed review、任务仓 dirty、开发线 dirty **都不**加严 ready set。
- `gate_log` 在 gate argv 哈希或被测树内容哈希变化后**展示**为 `decayed`。`0.7` merge **不**因这条新拒绝；现有 merge 本来就不重跑 gate。
- Trigger 观察有 TTL（`0.8+` 才派生）；过期只唤醒规划，不解除依赖，也不进入 `progress_fingerprint`。
- Objective 在 Task 合约或依赖闭包漂移后必须 reconcile，才能再 mutation。

衰减不是 cron。衰减是证据上的熵。续航引擎的时钟首先用来**宣布死亡**，其次才用来唤醒。展示衰减 ≠ 新的 merge 拒绝。

### 定律 III · 单一写入者

一个 `conflict_group` 里恰好有一个写入者。其余角色是见证者或导航者。

- 并行来自 ready set 与冲突组，不来自「再开五个 agent」。
- dispatch、Witness、Console 都不是写入者。
- review adapter **不是开发线或任务源码的写入者**；它必须能写 `review.md`。
- 宿主 agent 可以把 `dyro next` 的命令读给人看；它不能扩大命令的权限。

### 定律 IV · 编译后的权威

Agent 拿到的是法律的投影，不是法律的钥匙。编译器可以收缩权威，绝不能扩大。

- Core 拥有 mutation。
- Host Compiler 生成的 skill / hook / `AGENTS.md` 只能观察，或打印已经过权威交集批准的下一条命令。
- 投影里出现 `merge` / `push` / `signoff` 字样，不等于宿主获得了这些权。权仍在 CLI 的确认与策略交集里。

---

## 3. 分层：物理、能力、投影

```text
                    ┌─ Home / Console ──────────┐
                    │ 导航 · 注意力 · 零交付权   │
                    └────────────▲──────────────┘
                                 │ 只读投影
┌─ Host Compiler ──────────────────────────────────┐
│ 把定律编译成 SKILL.md / hooks / AGENTS.md         │
│ 只含本机可用能力 · 只收缩不扩张                   │
└────────────────────────▲─────────────────────────┘
                         │ Capability Card
┌─ Capability Plane ───────────────────────────────┐
│ Agent · Gate · Reviewer · Trigger · Tool         │
│ 声明能做什么、不能证明什么、能证明何种隔离       │
└────────────────────────▲─────────────────────────┘
                         │ Proof Object
┌─ Core Physics ───────────────────────────────────┐
│ Line · Task · Attempt · Proof · Decay · Integrate│
│ Objective / Snapshot / Plan / Lease（已落地）     │
└────────────────────────▲─────────────────────────┘
                         │
              Profile（团队法律，不进 Core）
```

三层禁止塌缩：

| 层 | 可以 | 禁止 |
| --- | --- | --- |
| Core Physics | 判定真值、衰减、集成、授权交集 | 内嵌客户仓库名、模型价、业务检查单 |
| Capability Plane | 描述本机能力与证明边界 | 把「已发现的 CLI」自动升级为可执行 adapter |
| Host Compiler | 投影定律到宿主 | 给宿主 merge/push/signoff 实权 |
| Home / Console | 让人看见下一件该做的事 | 成为第二份状态库或调度器 |

---

## 4. 新原语

这些原语**抽取**已有隐式结构，不另造平行真源。`task.toml`、compiled TaskGraph、receipt、review 绑定、以及每次重建的 `SchedulerSnapshot` 仍然是权威。`ContinuationSnapshot` 是未实例化的死类型，不是采样或 journal 真源。

### 4.1 Proof Object（证明物）

Proof 是带衰减函数的、可哈希寻址的事实记录。它不是又一份 `review.md`，而是所有已验证事实的统一投影。

```text
Proof
  id                   稳定 ID（kind + subject + generation + 无时钟身份载荷）
  kind                 见下表；0.7 只派生标了「0.7」的五种
  subject              task_id | line_id | changeset_id | objective_id
  substrate            repo heads · plan_sha256 · attempt_id · contract_hash
  procedure            可复现的核验程序（内置或 argv）
  bytes_sha256         被核验字节的哈希
  produced_at          只取记录内字段；缺则空。禁止 mtime / 「现在」
  declared_key_ids     导出时已声明的签名密钥 ID；未声明则为空
  policy_require_signed 导出时的 require_signed_review / require_signed_signoff 快照
  decay                见 4.2
  status               live | decayed | inconclusive | revoked
```

`generation` 使用已有证据世代 ID（或本地 attempt 世代）。身份哈希不含 `produced_at`、mtime 或「现在」。`produced_at` 只取记录内已有字段：`signoff.json` 的 `signed_at`、signed review JSON 的 `created_at`、action receipt 的 `created_at`。`review.md`、receipt、gate 日志、`integration_heads` 无记录内时间 → 空，不伪造，也不把文件系统 mtime 写进身份。

`contract_hash` 按 subject 拆，已锁定：task 面 kind（`gate_log` / `review_verdict` / `signoff` / `integration_heads`）用 attempt 的 `task_contract_sha256`（缺则该字段空，不得伪造）；`action_receipt` 用 Objective 的 `contract_sha256`。`proof list` / `verify` 每次全量重派生；store 只是可丢弃缓存，不是展示真源。

映射到现状：

| 已有物 | Proof kind | 列车 |
| --- | --- | --- |
| 编排器重跑的 gate 日志 + receipt | `gate_log` | 0.7 派生 |
| `review.md` + receipt/heads/attempt/plan 绑定 | `review_verdict` | 0.7 派生 |
| `signoff.json` | `signoff` | 0.7 派生 |
| 依赖 HEAD 已是线 HEAD 祖先 | `integration_heads` | 0.7 派生 |
| Continuation Action receipt | `action_receipt` | 0.7 派生；不进 `proof list --task` |
| 外部 evidence ZIP 世代 | `external_bundle` | 已有证据包的投影，不是 P6 Proof Bundle 的别名 |
| TriggerObservation | `trigger_observation` | 0.8+；字段跟 `next_probe_at`，不发明 `valid_until` |

源路径、id 公式、substrate 与 `produced_at` 规则见实施计划**附录 A**。

产品命令：

```bash
dyro proof list --task API-101
dyro proof list --objective OBJ-1
dyro proof show <proof-id>
dyro proof verify <proof-id>
dyro proof export <proof-id> --bundle /tmp/API-101.proof.zip
dyro proof export --task API-101 --bundle /tmp/API-101.proof.zip
dyro proof verify-bundle /tmp/API-101.proof.zip --git-dir /path/to/objects
```

`export` 的位置参数是 **proof-id**。按任务批量导出必须用 `--task`，与位置参数互斥。`proof list --task` **不含** `action_receipt`（它在 Objective `action-receipts/`）；要列 receipt 用 `--objective`。

两条核验命令、两套结论：

| 命令 | substrate | 结论含义 |
| --- | --- | --- |
| `proof verify` | **当前工作区** | `decay(proof, current_workspace_substrate)`。默认 rebind，不重跑 gate argv。 |
| `verify-bundle` | 捆内**钉死**的 substrate + `--git-dir` | **完整性**：字节哈希 + 钉死 SHA 在调用方对象库可解析。 |

`verify` 的 `--rerun-procedure` 才重跑，且必须 dry-run 或隔离。`verify-bundle` 无 `--current-heads` 时不得报与 merge 相同的衰减结论；缺省只能得出完整性意义上的 `live` 或 `inconclusive`，不能假装「现在还能 merge」。捆内不塞 git 对象，不含绝对路径、凭据、adapter env、prompt。拒绝把 `task evidence build` 的 ZIP 布局当成 Proof Bundle。缺 procedure、缺 substrate、缺调用方 git 对象、或缺已声明的签名密钥（导出时 `policy_require_signed=true` 且 `declared_key_ids` 为空）→ `inconclusive`，不得写成 `live`。这不是身份证明；Ed25519 信任根仍在现有 trust store。

视频、agent 摘要、多模型「都觉得可以」**不是** Proof kind。它们最多进入 dispatch 的建议信封。

### 4.2 Decay Clock（衰减钟）

每个 live Proof 带一个纯函数：

```text
decay(proof, current_substrate, clock) -> live | decayed | inconclusive
```

规则（Core 固定，Profile 只能加严）。**0.7 对 merge / 下游释放的接受与拒绝，必须与 `0.6.0` 现有绑定检查同真值**（A1）。衰减是这些检查的投影和 reason code，不是第二套门。`merge_task` 与 `check_dispatchable` **不**读取 Proof store。

谓词必须全量投影，禁止只比「task HEAD ≠ 绑定 HEAD」：

| kind | `live` 当且仅当 | `decayed` | `inconclusive` |
| --- | --- | --- | --- |
| `review_verdict` | `_valid_review_acceptance` 为真 | 绑定/HEAD/哈希变了，或 local 任务仓 dirty（已在 `_collect_task_heads`） | 缺 `review.md` / receipt / `task-heads.json`，或不可解析 |
| `signoff` | `_valid_external_signoff` 为真 | review / heads / attempt / plan 失绑，或策略开启时签名失效 | 缺 `signoff.json`、缺工具、缺已声明密钥 |
| `gate_log` | argv 哈希与被测树内容哈希仍匹配（**仅展示**） | 上述哈希变化（**仅展示**；merge 不新拒绝） | 缺日志 / 缺 generation |
| `integration_heads` | `_assert_dependency_integrated` 为真 | 线 HEAD 不再是证明 commit 的后代（reset / 换历史） | 缺 git / 缺 `task-heads.json` |
| `action_receipt` | 对应 receipt 字节与 journal 字段仍在 | 字段或世代被替换 | 缺文件 |

补充：

1. 开发线 dirty / 错分支保持 `_prepare_merge` 现有错。禁止标成 `PROOF_DECAYED`。`require_clean_merge` 只是加载期不变量。
2. `0.6` **已经**拒绝任务仓 dirty；`0.7` 保持。Proof 可把该失败投影为 `review_verdict` 的 `decayed` / `inconclusive`，不得改为 accept，也不得再叠第二道门。
3. `git revert` 仍留下后代提交，`integration_heads` **不**因此衰减。
4. `trigger_observation`：`0.8+` 才派生。若派生，用现有 `next_probe_at`，只影响唤醒，不影响完成，也不进入 `progress_fingerprint`。
5. 用户或策略显式撤销 → `revoked`。这不是 `decay()` 的返回值。

planner 在构造 **`SchedulerSnapshot`** 时评估衰减（不是未使用的 `ContinuationSnapshot`）。`progress_fingerprint` 的纯函数契约继续忽略 trigger；该函数已锁，但生产 `_budget_usage` **尚未**接线 `decide_no_progress`。`0.7` 不把 Proof 接入生产 `BudgetUsage`，不新开 no-progress 自动耗尽。merge 相关 live Proof 若投影，只进已有 `effective_evidence` / `integration_heads`，不并排再加一层。

`PROOF_DECAYED` **仅当**对应 Proof 从 `live` → `decayed` 且该衰减挡住的是 **merge** 人话。不得用它命名线 dirty / 错分支 / push 失败，也不得用它 block 下游 ready set。状态字段本身仍不是放行证据。

别人把「证明」写成交接故事；我们的 Proof 会过期。

### 4.3 Capability Card（能力卡）

今天的 `[adapters.codex]` 只有 argv。本机发现的 `opencode` / `cursor-agent` 若未审计，进不了执行面。Capability Card 统一描述**任何可被 Core 调用或拒绝的能力**。

```toml
[[capabilities]]
id = "codex"
kind = "agent"
preset = "codex"

launch = ["codex", "-C", "{workspace}"]
read   = ["codex", "exec", "--sandbox", "workspace-write", "{prompt}"]
write  = ["codex", "exec", "--sandbox", "workspace-write", "{prompt}"]

attested_isolation = "cwd"          # none | cwd | worktree | os_sandbox | external_runner
trusted_usage      = false          # 不能证明用量则禁止硬限额自动跑
can_prove          = []             # 只能填 Proof kind；空表示输出不能当完成证据
cannot_prove       = ["done", "merge", "security", "product_acceptance"]
intents            = ["observe", "execute"]
hosts              = ["cli"]        # cli = Dyro 启动的 adapter；不是宿主 skill 目录
```

字段语义：

| 字段 | 含义 |
| --- | --- |
| `kind` | `agent` / `gate` / `reviewer` / `trigger` / `tool` |
| `attested_isolation` | 能力**自称且可被 doctor 探测**的隔离上限。`cwd` 不是 OS 隔离。`strict` dispatch 仍要求 `os_sandbox` 或 `external_runner`。 |
| `can_prove` | 它的输出里，哪些可以变成 Proof。只填 Proof kind，不填 dispatch 词汇。 |
| `cannot_prove` | 即使它写了「已完成」，Core 也不得采信。 |
| `intents` | 它可请求的操作格：`observe` `execute` `review` `sign` `integrate` `publish`。 |
| `trusted_usage` | 是否能返回可核验用量。false 时 hard-limit 自动执行 fail-closed。 |
| `hosts` | 允许被编译到哪些宿主表面。 |

兼容：`0.7` 仍只读 `[adapters.*]`，不解析 `[[capabilities]]`。`0.8` 才运行时升级为 Card，缺省 `cannot_prove = ["done","merge"]`，`attested_isolation = "cwd"`。`dyro agent add` 在 0.8 继续工作，内部写 Card。

未审计的本机命令可以出现在 `dyro tool list` 和 Host Compiler 的「已发现未集成」区，**不能**获得 `execute` intent。

### 4.4 Host Compiler（宿主编译器）

常见编译器让 agent **更能干**。我们的编译器让 agent **更听话、更小、更不容易越权**。产品面叫宿主投影：给已审计宿主编译出 Skills 与可选 deny hook。

输入：

- 当前工作区的 live Capability Cards；
- 本机探测结果（已安装 / 已登录 / 未集成）；
- 用户路由偏好（不得指向未集成后端）；
- 四条定律的固定投影文本。

输出（按宿主 Card 声明的目录，原子替换）：

- `SKILL.md`：YAML 头（`name`、`description`，description 含负例）+ 何时调用 `dyro` + 禁止事项；
- 可选宿主规则片段：只含「交付以 Dyro 为准」和本机可用能力表；路径由 Card 声明，不在 Core 写死；
- 当宿主 Card 能证明拦截表面时，再投影一份由操作格编译出的 deny hook（见第 8 节）。

硬规则：

1. 只渲染本机可用且已审计的 Card。
2. 负例必须出现在 description 里（「不要用 git merge 结束任务」「不要把测试通过写成 done」）。
3. 编译产物不含绝对路径、remote、凭据、adapter env。
4. 编译器哈希写入 `.dyro/host-projections/<host>.toml`；宿主文件被手改后 `dyro host doctor` fail-closed。
5. 投影过期（Card 变更、工具消失、策略收紧）后，下一次 `objective` mutation 前必须重编译或显式跳过并记录 attention。

```bash
dyro host compile
dyro host status
dyro host doctor
```

Skill 健康检查并入 `dyro host doctor`：能发现、能拒绝过期投影，不当作「装得越多越好」。默认只写**当前工作区**下的投影目录。写用户级目录（例如 Codex 的 home skills）必须显式 `--user`，并在 doctor 标 `scope=user`。

---

## 5. 操作格（Intent Lattice）

所有写动作落入六格。Capability、Objective、ActivationLease、Task 合约、工作区策略的交集决定有效格。

```text
observe   →  status / graph / proof show / console
execute   →  task run / dispatch（建议）
review    →  独立主体，绑定 live Proof
sign      →  外部签收；执行主体不可签自己
integrate →  事务式本地 merge；仍走 `_valid_review_acceptance`，不读 Proof store
publish   →  push / 发布；第一版仍显式，且默认关
```

这不是命令名黑名单，也不是 prompt 里的权限档。它是 **Core 状态机的对外语言**。Host Compiler 只能把宿主放到 `observe`，外加「打印一条已批准的用户命令」。

---

## 6. 与已落地子系统的关系

| 子系统 | 保持 | 本设计增加 |
| --- | --- | --- |
| TaskGraph / 状态机 | 唯一交付图 | Proof 投影；0.7 衰减与现有 merge / 祖先检查同真值，只多 reason code |
| Objective / Continuation | 快照、计划、租约、预算 | `SchedulerSnapshot` 纳入 live/decayed Proof 投影；reason code `PROOF_DECAYED`（attention / 人话，默认不 block 下游）。journal 不存 proofs 当 PASS |
| dispatch | 建议、locator、租约 | Card 的 `attested_isolation` 替代口头 strict |
| Console / Home | 只读；summary 零新 git I/O | `0.8` 起展示 Proof 状态与衰减原因，不展示 argv/路径。`0.7` 用 `dyro proof list` 与 `dyro objective attention` |
| Witness | 追加哈希链 | Proof export 与 ledger 事件对齐；不把 Witness 当完成证据 |
| Blueprint / join | SHA 钉死的线 | 新队友得到的投影由本机 Card 编译，不携带源机工具清单 |
| Tool catalog | 打开工作区 ≠ 执行权 | 发现结果喂给 Compiler，不喂给 scheduler |

不新增第二份 backlog、第二份图、第二份完成状态机。

---

## 7. 用户体验

默认路径仍然是少概念：

```text
dyro                 看见线、目标、一条安全下一步
dyro next            打印唯一安全命令
dyro continue        按租约推进（已有）
dyro proof verify    需要争辩「到底做没做完」时
dyro host compile    换机器或换工具后
```

新人不必先懂 Proof 或 Card。他们撞上衰减时，attention 说的是人话：「复核已经失效，因为 api 的 HEAD 动了。下一步：重新复核，不要合并。」

专家路径：

```bash
dyro proof export <proof-id> --bundle ./API-101.proof.zip
dyro proof export --task API-101 --bundle ./API-101.proof.zip
dyro objective attention <objective-id>
dyro capability test codex
dyro host doctor --format json
```

---

## 8. 权威投影：锁定为 B

已选定 **skill 必编译，hook 按宿主能力可选投影**。不再保留 A/C 作为并行实现。

### 为什么不是 A

A 诚实，但 fort 只建了一半。宿主若直接 `git merge` 进开发线 worktree，Core 的 merge 预检能发现脏状态，可线已经被改写。定律 IV 要求编译器在能收缩权威的地方收缩；只写负例是把已知的机械漏洞重新交给 prompt。

### 为什么不是 C

C 看起来最严，其实是假完美。

1. Hook 不是 OS 边界。换路径、换包装脚本、走 IDE 内部 API，都能绕过。把 compile 建立在 hook 上，等于把「拦截点」宣传成隔离证明，和「`cwd` 不是沙箱」是同一类错误。
2. 强制 hook 会把无拦截表面的宿主逐出投影，Dyro 会绑死在少数几家工具上，变成跟跑者。
3. 不支持 hook 的宿主仍需要定律投影。拒绝 compile 等于把它们推向手写、过期、越权的 skill。

### B 的精确形状

`project_host_authority()` 只做这一条：

1. **所有宿主**都编译 skill / 规则投影（定律、负例、本机可用表、只打印已批准的 `dyro` 命令）。
2. **仅当**该宿主的 Capability Card 声明并被 `capability test` 证明存在 hook 表面时，再编译一份 deny hook。
3. Deny 清单从操作格编译，不从命令名黑名单手写：拦截未获授权的 `integrate` / `publish`，以及直接改 `.dyro/`。具体 argv 是投影，意图格才是真源。
4. 没有 hook 的宿主：`host compile` 仍成功，`host status` 标记 `authority_projection=skill_only`，doctor 不因此失败。
5. 曾经编译过 hook 的宿主，hook 文件被删或哈希漂移：`host doctor` fail-closed，自动 mutation 降为 plan-only。
6. 对外文案禁止把 hook 说成沙箱、隔离或「已阻止越权」。它是座椅安全带，不是车身。

Core 仍是唯一 mutation 权。Hook 挡不住的越权，仍由 dirty / HEAD 漂移 / decayed Proof 在交付门失败。

---

## 9. 安全不变量（本设计新增）

在架构文档既有 13 条之上增加：

14. 任何界面或宿主投影不得把 decayed / inconclusive Proof 显示为通过。
15. Capability Card 缺少 `cannot_prove` 时，默认至少包含 `done` 与 `merge`。
16. Host Compiler 的输出哈希必须可重算；手改投影在 doctor 中失败，不在运行时「尽量兼容」。
17. Proof Bundle 不得包含工作区绝对路径、remote URL 中的凭据、adapter 环境、prompt 或 answer。
18. `verify-bundle` 在缺 procedure、缺 substrate、缺调用方 git 对象、或缺已声明的签名密钥时返回 `inconclusive`，退出码与 `live` 区分。捆内不塞 git 对象。该 `live` 是完整性结论，不是「现在能否 merge」。
19. 发现到的未审计命令不得写入可执行 Card，也不得被 Objective 自动选中。
20. 密钥缺席：Card 不得声明看起来像秘密的环境变量名；需要认证的工具使用用户已登录的本机 CLI 会话，或独立的本机经纪，不把 token 写进 Profile。

---

## 10. 非目标

- 不把议题跟踪器做成 Core。它们若出现，只能是 Trigger provider，且观察结果不能完成 Task。
- 不实现提示词市场、角色包、或把技能数量当产品。
- 不实现舰队桌面 IDE、手机遥控、浏览器写权。
- 不把外部工具协议提升为控制协议。工具协议只连工具；完成权留在 CLI。
- 不在第一阶段自动 push、自动发布、自动创建远端仓库。
- 不把 dispatch 结果、模型共识、视频、截图升级为 Proof。
- 不把容器或云沙箱做成 Core 依赖。隔离后端继续走 Card 声明与 entry point。
- 不在仓库内保存「我们学了谁 / 对标谁」的对照附录。反模式用机制描述即可。
- 不把 `git revert` 当成祖先断裂。祖先检查只回答「提交是否仍在历史上」。
- 不把 Proof Bundle 做成自含 git 对象库。1.0 核验完整性，不核验身份。
- 不把 `verify-bundle` 的完整性结论说成与当前工作区 `proof verify` / `task merge` 同一套 `live` / `decayed`。
- 不把 `0.7` 写成「不拒绝任务仓 dirty」。那是对 `0.6` 的假描述；保持拒绝即可。
- 不把 `task evidence` ZIP 当作 Proof Bundle。

---

## 11. 为何这能争第一，而不是追第四

市场会继续比模型、比 star、比谁的 TUI 能并排开十二个 session。那条赛道没有终点，也没有我们的存量优势。

我们的不可复制点是已经连在一起的四件事：

1. **多仓开发线**是时间原子；
2. **证据绑定 HEAD** 且集成才释放下游；
3. **续航在图上走**，不在对话里走；
4. **投影只收缩权威**。

把这四件事收成 Proof、Card、Compiler，Dyro 的品类就从「agent 工具」锁成 **Delivery Physics**。后来者可以抄命令名，抄不走你们已经强制的衰减与跨仓祖先检查。

第一名的判据不是日活 agent 数，而是：

> 一个没参加过这次开发的人，拿着 Proof Bundle 和**自己提供的** git 对象，能否独立得出与源机**相同的完整性结论**（字节仍在、钉死 SHA 可解析、已声明密钥仍在）。这不是身份证明，也不是「现在工作区还能 merge」。当前能否 merge 只由工作区上的 `proof verify` / 现有绑定检查回答。

谁先让这句话成立，谁就定义了品类。
