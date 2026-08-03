# Dyro 持久续航引擎设计

状态：提案
目标版本：0.6.0；分阶段实现与验证，默认关闭自动运行
适用范围：Dyro Core、全局 Home、Profile 扩展边界

## 1. 产品判断

Dyro 当前已经能回答“哪些工作可以并行、按什么依赖交付、凭什么完成”。新增能力只回答另一组问题：

- 一个跨小时或跨天的目标下一次何时继续？
- 当前应该执行、复核、合并、等待、询问、暂停还是修复？
- 进程或机器重启后，如何从可证明的状态继续？
- 怎样防止无限重试、无效消耗和重复写入？
- 新用户如何只用一个入口理解当前最重要的动作？

因此产品定位是：**在既有交付图上增加受控的时间维度**，不是再造任务系统，也不是让 Agent 自己决定完成标准。

## 2. 设计目标与非目标

### 2.1 目标

1. 长期目标持久化，可跨进程、重启和换机后的受控恢复。
2. 同一输入快照得到同一计划；每个决定都可解释、可复现。
3. 所有自动动作有权限上限、预算、超时、租约和停止条件。
4. 等待外部事件时低成本退避；状态变化时及时唤醒。
5. 所有交付动作复用现有 TaskGraph、attempt、gate、review、signoff 和 merge。
6. 初学者不编辑配置即可开始；专家仍有完整 CLI 与 JSON 接口。
7. 本地优先、无遥测、无云服务强依赖、无供应商绑定。
8. 组合图视图可显示任务依赖、决策阻塞、唤醒条件和运行约束。

### 2.2 非目标

- 不生成或复制 Task 定义。
- 不让 Objective 成为第二份 backlog。
- 不通过循环次数或 Agent 文本判定交付完成。
- 不提供任意 shell 轮询器、通用定时任务平台或凭据托管。
- 不自动发布或推送；第一版自动化最多到经过多重授权的本地 merge。
- 不允许外部运行器、触发器或通知通道拥有 gates、review、signoff、merge、push 权限。

## 3. 核心领域模型

### 3.1 Objective

`Objective` 是长期意图的稳定边界，只引用一条开发线中的目标 Task：

```toml
schema_version = 1
id = "release-readiness"
title = "Complete the release readiness tasks"
line = "release-2026-10"
targets = ["API-101", "WEB-204"]
completion = "all_targets_integrated"

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]

[budget]
max_actions = 20
max_attempts_per_task = 2
max_failures = 3
max_no_progress_cycles = 2
max_parallel = 1
deadline = "2026-10-02T12:00:00Z"
```

约束：

- `targets` 在创建时固定；其依赖闭包由 TaskGraph 动态计算。
- Objective 的有效 mutation scope 是 targets 加其完整依赖闭包；创建向导必须把两者都预览给用户。
- Task 标题、仓库、依赖、gates、Agent、merge 权限不复制到 Objective。
- 新增目标必须走 `objective scope add` 或修改后执行 `objective reconcile`。
- 合约内容哈希与已接受 revision 绑定；未 reconcile 的漂移只允许只读检查。
- start 或 reconcile 会固定 Objective 合约哈希、依赖闭包和闭包内 Task contract 哈希；Task
  合约或依赖闭包变化后，下一次 mutation 前必须再次 reconcile。状态、attempt 和证据的正常
  变化不属于 contract drift。
- 默认完成条件要求所有目标 Task 的已验证 HEAD 已进入开发线。

### 3.2 ContinuationSnapshot

一次规划只读取一次所有权威输入，构造不可变快照：

```text
Objective contract + accepted revision
        │
        ├─ compiled TaskGraph + graph digest
        ├─ task status + task contract digest + integrated heads
        ├─ attempts + claims + active operation ownership
        ├─ decisions
        ├─ trigger observations
        ├─ budget balances and reservations
        ├─ workspace policy + activation lease
        └─ explicit UTC clock value
                         ↓
               ContinuationSnapshot
```

快照采用规范化 JSON 计算 `snapshot_sha256`。规划器不得在计算过程中再次读取文件、网络或时钟，避免同一轮出现撕裂视图。

### 3.3 ContinuationPlan

纯函数 `plan(snapshot) -> plan` 输出：

- `selected_actions[]`：本轮可执行的有序动作；
- `blocked[]`：被阻塞的对象、稳定 reason code 和事实；
- `attention[]`：需要用户理解或处理的投影视图；
- `completion`：尚未完成、完成或无法继续；
- `next_wake_at`：下一次有意义的检查时间；
- `snapshot_sha256` 与 `plan_sha256`。

动作类型：

| 类型 | 含义 | 第一阶段是否可自动执行 |
| --- | --- | --- |
| `execute_task` | 启动或重试 ready Task | 仅监督模式确认后 |
| `review_task` | 调用现有独立复核路径 | 仅监督模式确认后 |
| `merge_task` | 调用现有事务式本地 merge | 后期且需三层授权 |
| `probe_trigger` | 执行只读、有界的触发器探测 | 是 |
| `wait` | 等待 `next_wake_at` 或状态事件 | 不产生 Agent 消耗 |
| `ask_user` | 需要答案、决策或授权 | 否 |
| `pause` | 预算、deadline、无进展或策略停止 | 否 |
| `complete` | 目标完成条件已经由证据满足 | 派生结果；不写 lifecycle |
| `repair_required` | 存在不确定动作、损坏或漂移 | 否 |

规划器一次性为完整 action surface 计算 readiness：execute、review、merge、ask、Trigger、
wait 和 complete 都来自同一快照与同一 reason-code 表；daemon 不得再自行扫描 `review`
状态建立第二条队列。review acceptance 只改变受证据保护的 Task 状态，绝不隐式调用 merge
或 push；merge 永远是下一快照中的独立动作。

稳定 reason code 示例：`TASK_READY`、`DEPENDENCY_PENDING`、`DECISION_OPEN`、`ANSWER_REQUIRED`、`EXTERNAL_CLAIM_ACTIVE`、`TRIGGER_NOT_DUE`、`BUDGET_EXHAUSTED`、`NO_PROGRESS`、`CONTRACT_DRIFT`、`ACTION_UNCERTAIN`、`TARGETS_INTEGRATED`。

展示文本可以本地化，但 reason code、事实字段和排序不能依赖语言。

### 3.4 SchedulerTick 与 ActionIntent

`SchedulerTick` 是一次控制面评估；它可以只规划，也可以应用一个有界 wave。它不是 Agent attempt。

每个写动作在开始前发布不可变 `ActionIntent`：

```json
{
  "schema_version": 1,
  "action_id": "...",
  "idempotency_key": "...",
  "objective_id": "release-readiness",
  "objective_revision": 3,
  "snapshot_sha256": "...",
  "plan_sha256": "...",
  "operation": "execute_task",
  "task_id": "API-101",
  "status": "reserved",
  "owner_generation": 7,
  "expected_operation_generation": 2,
  "authority": {},
  "budget_reservation": {},
  "created_at": "..."
}
```

持久化阶段：

```text
intent(reserved) → action-start(started) → process-start(running)
                                      └→ receipt(succeeded|failed|uncertain|cancelled)
```

- create-only intent 本身就是预算 reservation 的权威记录；intent 发布前不启动任何副作用。
- create-only action-start 是副作用线性化点；它必须在 worktree、Task 状态或子进程发生任何
  写入之前 fsync。没有 action-start 的 intent 才能被安全取消。
- 需要启动命令时，Dyro 使用自有 launch barrier：受控启动器先记录 PID、进程启动代际、
  process group、deadline 和 action binding 并 fsync，再放行目标 argv。父进程提前退出时，
  未放行的目标命令不会执行。
- 成功或失败写不可变 receipt，再原子更新投影。
- action-start 后缺少可验证终态时进入 `uncertain`；恢复器不会自动重复运行。
- `repair` 只能执行预定义、可解释的恢复动作，不能运行任意命令。

idempotency key 绑定 Objective accepted revision、scope manifest、operation、subject、owner
generation 和预期 Task attempt 或 operation generation。同一副作用重复提交得到同一 key；
一次已终结的合法重试必须先推进 generation，得到新 key。

现有 `ExecutionAttempt` 保留为 Agent 工作单元，并增加可选的 `objective_id`、`action_id`、`snapshot_sha256` 和预算绑定。一个调度 tick 可产生多个互不冲突的 attempt，但不会引入新的“turn”证据体系。Task execution、gate 和 review 使用可注入的 managed-process context；普通显式命令保持现有同步接口。

ManagedProcessBackend 必须为目标操作系统证明“先持久化、后放行”和整棵进程树终止语义：
POSIX 使用控制管道与独立 process group，Windows 使用受控事件与 Job Object。后端自检未通过
时，Objective mutation fail-closed 为 plan-only；现有显式 Task 命令不被错误宣传为可恢复
managed execution。

### 3.5 Trigger

Trigger 只决定“何时重新规划”，不决定“交付是否完成”。

Core 内置：

- `time_due`：UTC 时间到达；
- `task_state`：目标或依赖 Task 状态变化；
- `decision_state`：决策点 resolved；
- `manual_signal`：显式命令提交的无秘密信号；
- `local_ref`：已登记仓库本地 ref 发生变化。

扩展 provider 可观察 CI、代码托管或其他系统，但必须返回统一的 `TriggerObservation`：`pending | satisfied | error | disabled`、观测摘要、证据引用、`observed_at`、`next_probe_at`。Core 对 provider 输出做 schema、大小、路径、超时和机密检查。Trigger 的满足只会唤醒重新规划或生成 attention，不能直接解除 TaskGraph 依赖、decision、gate 或证据要求；Objective 的 `not_before` 是独立的时间策略，不伪装成 Task 依赖。

退避规则：

- 未变化：指数退避并加入确定性 jitter，最大间隔受配置限制；
- 状态变化：立即写事件并重新规划；
- 可重试错误：记录分类和下次探测时间，不消耗 Agent action 预算；
- 永久错误或认证缺失：停止该 Trigger 并创建 attention；
- 同一观测摘要不重复写高层 ledger，避免噪声和磁盘膨胀。

扩展点使用可审计的 provider 描述和有界 JSON 子进程协议。协议不暴露任务状态变更、证据导入、复核、合并或推送操作。安装在同一用户权限下的第三方可执行程序仍属于受信任本机软件，Dyro 不把“协议隔离”宣传成操作系统安全边界；其 observation 无论如何都不构成交付证据。

### 3.6 Budget

预算采用“工作区上限、Objective 合约请求和本地 activation 批准值的交集”作为有效值。
ActivationLease 固定被批准的 Objective budget hash，并可携带只会收紧、不可能放宽的
`budget_limits`；没有额外 limits 时等于批准合约预算：

- 总 action 数；
- 每 Task attempt 数；
- 总失败数与连续失败数；
- 无进展 cycle 数；
- 单 action timeout 与 Objective deadline；
- 并行度和每 Agent 并发；
- 可选 provider 使用量。

执行前先保留最坏情况额度，receipt 后按实际用量结算或释放。对配置为 hard limit 的 provider 使用量，如果 adapter 不能返回可信 usage，则拒绝自动执行；不会以估算值伪装硬限制。Core 始终能强制 action 数、attempt 数、timeout、deadline 和并行度。

Objective 预算约束 Objective 驱动的动作。用户显式执行单个 `task run/review/merge` 仍保留人工
控制权，Objective 会在下一快照观察其结果，但不会伪造预算消费；无人值守的旧批处理命令
不得借此绕过 Objective 预算。

`progress_fingerprint` 只覆盖可证明的交付进展：Task 状态、集成 HEAD、决策状态和有效证据。Trigger 变化只负责唤醒，不会重置无进展计数，避免抖动观察器延长自动运行。连续 N 个 mutation cycle 指纹不变时自动暂停。

### 3.7 AttentionItem

Attention 是由快照和计划实时派生的视图：

- `ready`：可以安全推进；
- `waiting`：等待时间、外部证据或有效 claim；
- `needs_you`：问题、决策、确认或权限；
- `paused`：预算、deadline、activation 到期；
- `repair_required`：不确定动作、合约漂移、状态损坏或 dirty worktree。

稳定 attention ID 由 Objective、subject 和 reason code 计算。排序固定为：修复、需要你、可推进、暂停、等待。UI 可以保存“已查看”偏好，但该偏好不能改变底层状态。

### 3.8 与展示无关的只读投影

续航 Core 不允许 Home、JSON CLI 或未来的本地 Web 界面直接序列化
`ContinuationSnapshot`。PR-04 必须从同一快照生成不可变的
`SchedulerReadProjection`，PR-09 再生成 `AttentionReadProjection`：

- 只包含安全 ID、状态、稳定 reason code、经过类型约束的事实、预算摘要、下一唤醒时间、
  action 或 receipt 关联 ID 和组合图 digest；
- 不包含 workspace 绝对路径、argv、环境变量、prompt、answer、原始 provider 输出或日志正文；
- 不包含本地化后的展示文本；CLI、Home 和其他界面各自根据 code 渲染；
- 与来源 `snapshot_sha256` 绑定，同一快照必须产生相同规范化投影；
- 只在读取时派生，不持久化为第二份状态，不提供 mutation callback 或可执行命令句柄。

这样所有展示面都复用同一 readiness、attention 和事实集合，而不要求页面理解内部 journal、
锁或恢复结构。任何界面专属 DTO 只能在该投影之上继续做更严格的白名单和脱敏。

`objective attention` 只渲染该投影；它不创建 Action、不会执行或修复任务，也不读取未投影的
日志、prompt、answer、路径或资源值。用户配置的冲突组、决策和依赖标识会降维为布尔事实，而不是
原样显示。

## 4. 单一调度器与图模型

### 4.1 共享调度内核

重构现有调度为三层：

```text
build_scheduler_snapshot(config, scope, clock)
                 ↓
plan_scheduler(snapshot)
                 ↓
apply_plan(plan, authority, dry_run)
```

- `task next/loop/daemon` 使用 workspace 或 line scope；
- `objective plan/tick` 使用 pinned Objective scope；
- `dyro continue` 选择当前 Objective 后调用同一入口；
- `task run/review/merge` 仍是最底层受保护 mutation API。

这样 ready set、依赖集成、decision、claim、conflict group 和并发 wave 只有一套规则。

### 4.2 组合图视图

`dyro objective graph <id>` 从同一快照生成只读投影：

- Task 节点与 `depends_on` 边来自 TaskGraph；
- Decision 节点与阻塞边来自 `blocked_on`；
- Trigger 节点与唤醒边来自 Objective contract；
- active Action 节点引用 Task 或 Trigger；
- conflict group、预算与 lease 显示为约束，不伪造成依赖边。

Mermaid 与 JSON 必须来自同一图对象。这个视图不持久化为另一份图配置。

## 5. 状态与存储

```text
.dyro/objectives.lock                 workspace 级 scope/并发事务锁
.dyro/objective-ownership.json        可重建的 mutation ownership 投影
.dyro/objectives/<id>/
  objective.toml                    用户可审阅的目标合约
  state.json                        原子替换的当前投影
  activation.json                   0600 的本机自动运行授权投影
  checkpoint.json                   已验证事件序号与链头
  events.jsonl                      追加式、序号化事件
  ticks/<tick-id>.json              不可变调度轮次记录
  actions/<action-id>.json          不可变初始 intent
  action-starts/<action-id>.json    不可变副作用线性化记录
  process-starts/<action-id>/<n>.json 不可变进程身份与启动屏障记录
  action-receipts/<action-id>.json  不可变终态回执
  triggers/<trigger-id>.json        原子替换的最近观测
  .state.lock                       Objective 事务锁
```

关键规则：

1. 所有 ID 使用现有安全 ID 规则，拒绝绝对路径、`..` 和符号链接逃逸。
2. 小文件使用同目录临时文件、fsync、rename；不可变记录使用 create-only 发布。
3. `events.jsonl` 每项含递增 `seq`、前项哈希和本项哈希；损坏或截断 fail-closed。
4. `state.json` 是可重建投影，不是交付完成证据。
5. 只有 Objective 高层状态变化同步到 workspace ledger；轮询细节留在 Objective 日志。
6. 合约改变后，任何 mutation 都要求显式 reconcile，并记录旧、新哈希和 revision。
7. 详细 provider 原始输出不进入事件流；只保存受限摘要、哈希和安全证据引用。
8. intent、事件和投影无法跨文件原子提交时，以 create-only intent 为保守事实：恢复先发现
   未投影 intent，补记事件或进入 repair，再允许创建下一动作。
9. `objective-ownership.json` 只是从 accepted scopes 与未解决 actions 重建的投影；不确定动作
   未解决前不得释放其 scope ownership。
10. 全局 Home 的最近 Objective 存入独立、带版本的偏好文件，不向严格 v1
    `workspaces.json` 添加未知字段，旧版本可继续读取 workspace registry。

## 6. 权限与运行模式

### 6.1 操作者状态与运行模式

持久操作者状态只有 `active | paused | stopped`。`stopped` 是终态；继续工作应创建新
Objective，不能把停止伪装成完成。结果每次从当前 TaskGraph、HEAD 和证据重新派生为
`incomplete | complete | repair_required`，因此历史集成被回退或证据失效时不会继续显示
虚假的 complete，也不存在直接写 completed 的 API。

| 操作 | reserved | started/running | uncertain |
| --- | --- | --- | --- |
| pause | 取消未 start intent | 不再链式启动；等待 receipt 或 uncertain | 保留 ownership，需 repair |
| stop | 取消未 start intent | 不再链式启动；终态 receipt 仍可接收 | 保留 ownership，需 repair |
| reconcile | 先取消后执行 | 拒绝 | 拒绝 |
| activation revoke/expire | 取消自动 intent | 当前动作可结束，禁止后续动作 | 保留 ownership，需 repair |

续航运行模式是另一维度：

| 模式 | 行为 |
| --- | --- |
| `observe` | 仅 plan、explain、graph、attention；绝不 mutation |
| `supervised` | 每个 mutation wave 显示精确动作并要求确认 |
| `automatic` | 在有效 ActivationLease 与操作 allowlist 内无人值守运行 |

创建 Objective 默认 `supervised`，但没有确认时等同 observe。自动模式必须通过显式命令设置过期时间：

```bash
dyro objective activate release-readiness --mode automatic --until 2026-10-02T12:00:00Z --yes
```

ActivationLease 绑定 workspace 身份、Objective ID、accepted revision、scope manifest、合约
哈希、workspace policy 哈希、approved budget 哈希、可选更严格 budget limits、operation
allowlist、签发者、本机 installation ID、签发时间和过期时间。`issue | renew | revoke |
expire` 都递增 activation generation 并记录事件；
`activation.json` 权限为 0600。Objective reconcile、scope 或 policy 变化会立即使旧 lease
失效。全局配置目录中的随机 installation ID 同样使用 0600；复制 workspace 到另一台机器不会
携带无人值守权限。它是可跨同机进程重启的本地授权记录，不是抵御同一操作系统用户恶意程序
的秘密令牌；Objective owner lease 另行负责单进程调度所有权。持久状态记录 `last_seen_at`，
发现超出容差的时钟回拨时自动授权失效，需重新 activate。

### 6.2 有效授权矩阵

| 动作 | workspace | Objective | local activation | Task 或证据 |
| --- | --- | --- | --- | --- |
| execute | 允许无人值守执行 | operations 包含 execute | lease 有效 | ready + locks |
| review | 允许无人值守复核 | operations 包含 review | lease 有效 | review 状态 + HEAD 固定 |
| merge | 允许无人值守本地 merge | operations 包含 merge | lease 有效 | `merge.auto=true` + PASS 绑定 |
| push | 不提供自动授权 | 无效 | 无效 | 始终要求显式命令 |

任一层缺失都降级为 attention，而不是自行放宽。

### 6.3 外部执行模式

在 `execution_mode = "external"` 时，控制机上的续航引擎只能：

- 规划与解释；
- 探测 claim 是否过期；
- 等待并提示导入执行或复核证据；
- 观察决策和 Trigger；
- 生成 attention。

它不能代替 runner 执行 gates，不能代替 reviewer，不能根据远程状态直接完成 Task，也不
调用 evidence import。外部模式把认证和身份独立提升为 Core 不变量：signed execution 与
signed review 都是必需项，需要 signoff 时 signed signoff 也必需。trust metadata 为每把 key
绑定不可变 principal，签名 payload 的 actor 必须匹配该 principal；runner 与 reviewer
principal 必须不同，需要 signoff 时 policy 还可要求 approver 与二者不同。execution、review、
signoff 继续使用分用途 trust store 和签名 domain。旧的宽松配置由 doctor 给出迁移错误，不
静默兼容。

## 7. 崩溃恢复与并发

### 7.1 Objective owner lease

每个自动调度进程取得带 owner token、单调递增 fencing generation、PID、进程启动标识、
heartbeat 和到期时间的 Objective lease。每次新取得或过期接管都递增 generation；只有 token
与 generation 完全匹配者可续租和释放。租约失效只代表调度权可接管，不代表正在运行的副作用
可以重放。

在 `intent → action-start` 之间，启动器必须在 workspace Objective 锁下重新验证：操作者状态、
accepted revision、scope manifest、policy digest、activation generation 或监督确认、owner token
与 fencing generation、budget 和 task expected generation。create-only action-start 是唯一
线性化点。pause、stop、reconcile、policy 变化或 lease 接管会取消尚未 start 的 intent；已经
start 的旧 generation 动作可提交与其 start binding 匹配的终态 receipt，但不能创建下一动作。
例如 review 执行期间 lease 到期，只能接收 review receipt，不能顺带启动 merge。

涉及 Objective event 的取消计划与 event 同写入 pending transaction：只有 event 已持久化后才
materialize cancellation receipt；崩溃恢复会完成该计划，未提交 event 则丢弃计划且不改变 Action。
lease 接管使用独立 pending record 绑定新 lease 的完整内容；先持久化 lease，随后完成取消，恢复时
仅在新 lease 精确匹配时完成取消，若仍是接管前 lease 则丢弃计划，其余组合 fail-closed，避免在
新 owner 未成立时取消旧 intent。

### 7.2 恢复分类

| 发现状态 | 恢复动作 |
| --- | --- |
| intent 存在但没有 action-start | 安全取消；重新计划后由新 generation 决定是否重建 |
| action-start 存在，process-start 尚无且无 receipt | 标记 uncertain；不假设零副作用 |
| process-start 存在且能证明进程仍存活 | 等待并续查，不接管副作用 |
| action-start 存在且已有完整 receipt | 验证 binding，发布投影并继续 |
| process 消失且无 receipt | 标记 uncertain，停止自动化 |
| task 已进入后续权威状态 | 验证绑定后补写幂等 receipt |
| journal、contract 或 hash 损坏 | fail-closed，输出 repair 指南 |

### 7.3 并发资源

Action 在启动前声明资源：`task:<id>`、`conflict:<group>`、`agent:<id>`、`line:<id>:merge`。Planner 先生成完整 ready set；`objective tick` 再从同一快照生成可复核、无写入的 wave 预览，按资源和当前并行容量选择候选。已 `in_progress` 的 scope Task 和有效外部 claim 会先占用该 Objective 的并行容量。延期投影只公开受限的 resource class（task、conflict、agent、line），绝不公开资源值。后续 apply 阶段必须重新校验授权、预算、资源与现有 task、dispatch、merge locks，防止计划与执行之间的竞态。预览的 `tick_sha256` 绑定 snapshot、plan、容量、wave 和延期原因，不能当作执行授权或绕过重新校验。

一个 Task 可以被多个 observe-only Objective 引用，但同一时刻最多只有一个 active Objective
取得其 targets 加依赖闭包的 mutation ownership。ownership 在 workspace Objective 锁下预留；
重叠 Objective 只能观察并显示 `OBJECTIVE_SCOPE_CONFLICT`。执行 Task API 时不持有 Objective
锁，统一锁序为“Objective reservation 完成并释放锁，再进入现有 task/dispatch/merge 锁”，
避免跨层死锁。

`task next --run` 等显式单任务命令仍可由用户直接调用；当 `task loop` 或 `task daemon` 命中
active Objective 的 mutation scope 时，必须委托给同一 engine/action journal，或拒绝并提示
使用 `objective tick/daemon`。没有 Objective 的 workspace 保持原行为。

## 8. CLI 与新手体验

### 8.1 两条最短路径

```bash
# 交互创建：自动选择当前 line，列出并固定目标，使用安全预算
dyro objective start

# 显示并执行唯一安全下一步；监督模式先确认
dyro continue
```

### 8.2 完整命令面

```text
dyro objective start|list|status|plan|explain|graph
dyro objective pause|resume|stop|reconcile|activate
dyro objective tick|daemon|repair
dyro trigger list|probe|signal
dyro attention
dyro continue
```

所有读取命令支持 `--format text|json`；所有可能 mutation 的命令支持 `--dry-run`，且非交互环境必须显式 `--yes`。`objective stop` 表示用户停止，不冒充完成。

所有新增顶层命令共用 workspace resolver，顺序固定为：显式 `--root/--workspace` → 当前目录
Profile → 全局登记的默认或唯一 workspace → TTY 交互选择。非 TTY 遇到多个或没有可确定
workspace 时 fail-closed，并要求显式 selector。Objective 选择顺序为显式 `--objective` →
workspace 内唯一 active Objective → TTY 交互选择；非 TTY 多 active 时同样拒绝猜测。

`objective start` 的 line 选择顺序为显式 `--line` → 当前所在 line 或 task worktree → 唯一
line → TTY 选择；没有可判定 line 时给出创建或显式选择命令。`objective scope add/remove` 提供
无需编辑 TOML 的 scope 维护，但只有不存在 started、running 或 uncertain action 时才能在
workspace Objective 锁下改合约并 reconcile。

### 8.3 bare `dyro`

首页在现有 workspace 选择之后优先显示：

```text
当前目标：Release readiness
状态：需要你（1） · 可推进（2） · 等待（1）
建议：回答 API-101 的接口兼容性问题

1. 处理建议动作
2. 查看全部目标状态
3. 打开开发线或任务工作区
4. 选择编码工具
```

“继续目标”执行 Task contract 中指定的 Agent；“打开工作区”继续使用现有 coding-tool picker，两者不混淆。失败消息始终包含原因、影响和一个恢复命令。

### 8.4 新手默认值

- supervised；
- 1 个并行动作；
- 每 Task 最多 2 次自动 attempt；
- 连续 2 个无进展 cycle 后暂停；
- 24 小时 deadline；
- 自动 merge 和 push 均关闭；
- 无外部 Trigger、无后台服务、无配置文件编辑。

向导在应用前用一句话预览：“将跟踪 N 个任务；每次写操作先确认；不会自动合并或推送”。

## 9. 安全与隐私

- Provider 或 Trigger 配置只接受结构化字段和受审计的 argv；不执行 shell 字符串。
- Core 内置 Trigger 不访问任意 URL；远程访问由受限扩展提供并声明 host allowlist。
- 所有输出设字节、字段、列表和超时上限；解析失败 fail-closed。
- 凭据只通过现有环境或系统凭据机制进入 provider，不写 Objective 合约或日志。
- 对路径、符号链接、权限、日志注入和 Unicode 控制字符做统一校验。
- 使用 UTC 持久化时间；单进程间隔使用 monotonic clock；休眠或时钟回拨后重新规划。
- 默认不启用遥测；任何未来遥测必须单独 opt-in 且不包含任务内容、路径和凭据。
- 插件返回的是 observation，不是可执行 Python 回调句柄；Core 在边界复制并验证数据。

## 10. 可观测性与解释

每个 plan、tick、action 和 receipt 都能通过 ID 串联：

```text
objective revision
  → snapshot_sha256
  → plan_sha256
  → tick_id
  → action_id
  → existing attempt_id / review binding / merge ledger
```

`objective explain` 默认给人类原因；`--format json` 输出 reason code、事实、来源文件的安全相对定位、预算余额和下一唤醒时间。它不输出 secret、完整 provider stdout 或未经验证的远程正文。

最低指标只本地计算：计划耗时、Trigger 探测耗时、action 成功或失败或 uncertain、预算余额、无进展次数。第一版不上传。

## 11. 失败模式

| 失败 | 行为 | 用户恢复入口 |
| --- | --- | --- |
| 无 ready Task | WAIT，说明依赖、决策或集成原因 | `objective explain` |
| Agent 未安装或未登录 | needs_you，不自动换未授权 Agent | `dyro tool install` / `agent test` |
| Task contract 漂移 | repair_required，禁止 mutation | `objective reconcile` |
| dirty worktree | repair_required，不 stash 或 clean | `dyro task open` |
| Trigger 认证失败 | 禁用该 Trigger，其他路径可继续 | `trigger probe` |
| action crash | uncertain，禁止重复执行 | `objective repair` |
| budget 或 deadline 到达 | paused | `objective status` / `activate` |
| Objective journal 损坏 | fail-closed | `objective repair --check` |
| 外部 claim 过期 | needs_you，不接管外部执行 | `task claim` |
| 本地 merge 部分失败 | 复用现有事务恢复与 ledger | `task merge` / `doctor` |

## 12. 测试策略

### 12.1 纯逻辑

- 同一规范化快照的 plan 字节完全一致；
- 随机 TaskGraph 上不存在越过依赖、decision 和 conflict 的 selected action；
- execute、review、merge、ask、Trigger、wait 和 complete 共用 action readiness；daemon 不存在
  独立 review scan；
- reason code 对所有未选目标完整覆盖；
- budget、deadline、no-progress、activation 交集符合真值表；
- 图 JSON 与 Mermaid 节点或边集合一致。

### 12.2 状态与恢复

- atomic replace、create-only、fsync、锁超时、重复 event、事件断尾和 hash 分叉；
- 在 intent 前、intent 后、action-start 前后、launch barrier 前后、process-start 后、receipt
  前后逐点注入崩溃；
- 双 daemon、PID 复用、fencing 接管、旧 owner 发动作、pause 位于 intent 与 start 之间、
  review 中 lease 到期、sleep 或 wake、时钟回拨；
- uncertain 永不自动重放，repair 必须验证当前 task、HEAD 和 attempt。

### 12.3 安全

- 路径穿越、符号链接逃逸、非法 ID、超大输出、JSON 或 TOML 深度和字段上限；
- Provider 输出伪造 task done、review 或 merge 指令时被当作无权限数据；
- external mode 无法启动本地 Agent、gate、review 或 merge；
- external mode 拒绝 unsigned review、runner 自我复核和违反策略的 signoff 身份；
- dry-run 不创建 Objective、tick、budget reservation、recent state 或网络连接；
- `review_task` 在 `merge.auto=true`、Task push 请求和 workspace allow_push 全部打开时仍不会
  调用 merge 或 push；
- 自动 merge 缺少任一授权或证据绑定时 fail-closed；
- 自动 push 在所有路径均被拒绝。

### 12.4 端到端

- 新用户从任意目录创建 Objective、继续一个 Task、回答问题、复核、手动 merge；
- 进程重启后从 receipt 继续；从 uncertain 停止并给出修复动作；
- 时间、decision、manual signal、task state Trigger 的等待与唤醒；
- 外部执行只观察 claim 或证据并提示导入；
- 两个不重叠 Objective 共享 workspace concurrency 上限；重叠 mutation scope 只能有一个 owner；
- 从无关目录按 resolver 规则选择 workspace 或 Objective；非 TTY 歧义明确失败；
- 1000 Task 的本地 plan 不访问网络，且在约定基准内完成；
- wheel 或 sdist 在源码树外安装后，CLI、默认模板和所有资源可用；
- 全仓术语策略扫描覆盖源码、测试、文档、示例、生成帮助、构建产物元数据。

## 13. 发布门槛

1. 计划只读阶段通过确定性、图一致性和零写入审计。
2. 监督阶段通过 crash matrix、并发锁、budget reservation 和真实本地 Agent 冒烟。
3. 首页阶段通过新人可用性测试：无需编辑 TOML，三分钟内创建目标并理解下一步。
4. 自动阶段需单独安全评审，默认关闭，并验证 lease 到期即时降级。
5. 升级或降级测试证明：删除 Objective 不影响现有 Task，旧命令仍可工作。
6. `v0.6.0` 同时包含持久续航 PR-01 至 PR-12 和 Console C01 至 C06；控制台只读、
   自动运行默认关闭、自动 push 不存在，三者均为同一发布门禁的不可降低条件。
7. PR-09 之后，Console C01–C06 可与 PR-10、PR-11 并行推进；两线完成后由 PR-12
   汇合，执行统一的安全、迁移、产物和发布候选验证。
8. 构建产物在干净环境安装验证；版本、CHANGELOG、CI、包占用和发布审批走现有流程。

`v0.5.7` 仅接收维护修复，不承载上述新能力。统一发布只表示同一版本候选与同一最终
门禁，不降低每个阶段的独立出口条件，也不把自动运行变为默认行为。

## 14. 明确延期

- 自动 push、自动发布、远程凭据托管；
- 跨 workspace Objective；
- 任意 HTTP 或 shell Trigger；
- 自动修改 Objective scope 或验收条件；
- 分布式多控制面一致性；
- 用模型投票替代 gates 或独立复核。
