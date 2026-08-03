# Dyro 持久续航引擎实施蓝图

状态：待批准
规划基线：`main@489caaf68bbb0417bc1fa74c43fb100b9034af2b`（v0.5.6）
发布目标：Dyro 0.6.0；`v0.5.7` 只保留维护修复
交付方式：1 个设计记录 PR，加 12 个续航实现 PR 与 6 个 Console 实现 PR；所有实现 PR 均可独立评审、可独立回滚
默认策略：先只读、再监督、最后显式开启限时自动运行

## 1. 最终交付结果

完成本蓝图后，Dyro 将具备：

1. 引用现有 TaskGraph 的持久 Objective；
2. 不重复读取状态的规范化调度快照；
3. 确定性续航计划、稳定 reason code 与组合图视图；
4. 预算、无进展停止、Trigger 退避和下一唤醒时间；
5. 写前 intent、写后 receipt、uncertain 停机与受控恢复；
6. `dyro objective ...`、`dyro continue`、`dyro attention`；
7. bare `dyro` 的目标优先首页；
8. 限时 ActivationLease 下的无人值守执行与复核，以及独立门禁后的本地 merge；
9. 旧 Task 命令与新 Objective 命令共用一个调度内核；
10. 源码树外安装、升级、降级、安全与术语策略验证。

## 2. 实施原则

- 每一步只做一个 PR；依赖 PR 未合并前，不在后续分支重复实现其代码。
- 每个 PR 从其依赖的精确合并 SHA 创建独立 linked worktree。
- 不修改用户当前 checkout，不清理、不 stash、不重置任何已有改动。
- 先写失败测试，再实现最小闭环，再跑全量回归。
- 所有新增 mutation 均提供 dry-run、锁、审计、失败恢复和明确确认语义。
- 不直接写 Task 的质量门状态；只调用现有受保护 API。
- 第一版永不自动 push 或发布。
- 未达到阶段出口条件时，下一阶段保持关闭。

## 3. 目标模块地图

```text
src/dyro/continuation/
  models.py       不可变领域类型、枚举、reason code
  contracts.py    Objective 合约解析、验证、规范化哈希
  store.py        Objective 投影、事件链、不可变记录、scope ownership
  resolution.py   任意目录的 workspace/line/Objective 解析
  snapshot.py     一次性读取权威输入
  planner.py      纯计划器与组合图投影
  budgets.py      reservation、结算、deadline、no-progress
  triggers.py     内置 Trigger 与 provider 边界
  actions.py      intent、receipt、uncertain、repair 检查
  leases.py       Objective owner 与 ActivationLease
  managed_process.py 启动屏障、进程身份、进程树与有界输出
  engine.py       tick、apply、现有 Task API 编排
  attention.py    只读 attention 投影
```

现有模块的职责保持：

- `tasks.py`：TaskGraph readiness、任务状态、执行、复核、合并；
- `provenance.py`：ExecutionAttempt 与证据绑定；
- `state.py`：安全原子写、create-only、锁；
- `home.py`：全局入口和展示，不拥有交付权限；
- `cli.py`：参数解析和确认，不承载领域规则。

## 4. 依赖图与并行策略

```text
PR-01 Authority Hardening
   │
PR-02 Contracts
   │
PR-03 Objective Store & Resolution
   │
PR-04 Action-level Scheduler
   ├── PR-05 Pure Budgets ── PR-07 Journal, Fencing & Managed Process ─┐
   └── PR-06 Triggers ──────────────────────────────────────────────────┤
                                                                       ▼
                                                        PR-08 Supervised Engine
                                                                       │
                                                       PR-09 Attention & Home
                                      ┌────────────────────────────────┴────────────────────────────────┐
                                      ▼                                                                 ▼
                     PR-10 Automatic Execute/Review                                Console PR-C01 Read Model
                                      │                                                                 │
                     PR-11 Automatic Local Merge                                  Console PR-C02 Secure Runtime
                                      │                                                                 │
                                      │                                           ┌─────────────────────┴─────────────────────┐
                                      │                                           ▼                                           ▼
                                      │                              Console PR-C03 Overview                     Console PR-C04 Details & Graph
                                      │                                           └─────────────────────┬─────────────────────┘
                                      │                                                                 ▼
                                      │                                             Console PR-C05 Integrated UX
                                      │                                                                 │
                                      │                                             Console PR-C06 Hardening
                                      └────────────────────────────────┬────────────────────────────────┘
                                                                       ▼
                                                     PR-12 Unified 0.6.0 Hardening & Release Gate
```

依赖主线固定为 PR-02 → PR-03 → PR-04；仅 PR-05 与 PR-06 在 PR-04 后可并行。PR-07
依赖 PR-03、04、05，确保 create-only intent 能成为预算 reservation，而不是让预算与 action
两个 PR 并行争夺同一事务语义。PR-09 只在 PR-08 API 冻结后开始。自动 execute/review 与
自动 merge 分为 PR-10、PR-11 两道独立安全门。

本地 Web 控制台从 PR-09 分出只读交付线，消费 PR-04 的共享快照或图投影和 PR-09 的
Attention 投影，不反向拥有 continuation 状态或动作。PR-10、PR-11 可与 Console C01–C06
并行；但 PR-12 必须等待 PR-11 和 C06，作为 Dyro `0.6.0` 的唯一统一发布门禁。详细依赖和
文件所有权见 `plans/local-web-console-implementation.md`。

## 5. 分步计划

### PR-01：控制面权限前置加固

目标：先修复现有 review 与外部证据边界；在此门禁合并前，不开始续航运行时代码。

冷启动先读：

- `src/dyro/tasks.py` 的 `_apply_review_decision`、`review_task`、`import_review_evidence`、
  `signoff_task`、`merge_task`
- `src/dyro/reviews.py`、`src/dyro/signing.py`
- `src/dyro/cli.py` 的 daemon review queue
- `tests/test_tasks.py`、`tests/test_external_signing.py`

主要改动：

- 把“接受 review”与“执行 merge”拆为独立 Core 操作；`review_task` 和
  `import_review_evidence` 绝不调用 merge 或 push。
- `merge.auto` 只表示后续独立 merge action 的 task-level 许可；`merge.push` 不再触发隐式
  push，显式 `task merge --push` 仍受现有双重确认和 workspace policy 约束。
- external mode 强制 signed execution 与 signed review；需要 signoff 时也强制 signed signoff。
- trust metadata 增加不可变 `principal_id`，签名 payload 的 actor 必须等于对应 key principal；
  execution claimant 与 reviewer principal 必须不同。
- require signoff 时按 policy 验证 approver 与 runner、reviewer 的独立性；三个用途继续使用
  分离 trust store 和签名 domain。
- 外部 evidence import 保持显式控制面命令；不提供给 agent、Trigger 或后续 engine 自动调用。
- doctor 对旧的宽松 external Profile 给出迁移错误，不静默降级或继续完成 Task。
- 增加通用 terminology-policy scanner；deny list 从仓库外环境注入，输出 policy input hash，
  仓库内不保存敏感比较词或来源说明。设计记录、PR 分支、diff 和提交候选从本 PR 起必扫。

测试：

- `merge.auto=true`、Task push 请求和 workspace allow_push 都为 true 时，PASS review 仍不调用
  Git merge 或 push。
- unsigned execution/review、actor 与 key principal 不匹配、runner 自我复核、重复用途 key 和
  违反独立性 policy 的 signoff
  都不能进入 done。
- 合法 signed independent review 与可选 independent signoff 可完成 Task，但仍需独立 merge。
- 旧 local/manual 流程、review binding、task merge 事务恢复全部回归。

出口条件：review 和 merge 在代码、测试、ledger phase 与 CLI 上都成为两个独立动作；外部
执行身份无法给自己授予完成权限。

回滚：这是安全前置变更，不与 Objective 状态耦合；若不能兼容迁移，停止整个项目而不是在
后续 PR 绕开。

### PR-02：领域契约与包边界

依赖：PR-01。

目标：冻结术语、schema、权限交集和模块接口；不新增运行时 mutation。

冷启动先读：

- `docs/architecture.md`
- `docs/adr/0003-zero-friction-global-home.md`
- `src/dyro/config.py`
- `src/dyro/tasks.py` 中 Task、SchedulePlan、ScheduleWave
- `src/dyro/provenance.py` 中 ExecutionAttempt
- `pyproject.toml`

主要改动：

- 以已批准的 ADR、设计和实施蓝图为约束，不在代码 PR 中改写核心决策。
- 新增 `src/dyro/continuation/__init__.py`、`models.py`、`contracts.py`。
- 在 `config.py` 增加 `OBJECTIVES_DIR` 和可选 workspace 权限上限，缺省值保持关闭。
- 在 `pyproject.toml` 显式打包新子包。
- 定义 Objective、Snapshot、Plan、Action、TriggerObservation、BudgetLimit、AttentionItem
  的不可变类型和稳定 reason code；禁止在模型层读文件、时钟或环境。
- 定义 Objective v1 合约限制：单 line、显式 targets、依赖由 TaskGraph 计算、默认
  `all_targets_integrated`。

测试：

- 合法最小合约、完整合约、未知字段策略、类型边界和安全 ID。
- 空 targets、跨 line、重复 targets、非法 deadline、无限预算、未知 operation 拒绝。
- 规范化合约相同则哈希相同，语义变化则哈希变化。
- 从 wheel 安装后可 import `dyro.continuation`。

验证命令：

```bash
uv run --frozen python -m unittest tests.test_continuation_contracts
uv run --frozen ruff check src tests
uv build
git diff --check
```

出口条件：公开模型、schema 和权限矩阵评审通过；没有 CLI mutation；旧测试全绿。

回滚：整 PR 可回滚；没有生成任何 `.dyro/objectives` 状态。

### PR-03：Objective 存储、解析、ownership 与操作者状态

依赖：PR-02。

目标：可靠创建、读取、暂停、恢复、停止、调整 scope 和 reconcile Objective，并从任意目录
确定 workspace；仍不执行 Task。

冷启动先读：

- 已批准 ADR、设计与 PR-02 `continuation/models.py`
- `src/dyro/state.py`
- `src/dyro/tasks.py` 的 task_dir、list_tasks、ledger
- `src/dyro/cli.py` 的 parser、`_require_yes`、dry-run 约定

主要改动：

- `state.py` 增加安全 create-only 和目录 fsync helper，保留现有 API。
- 新增 `continuation/store.py`：Objective 目录验证、原子投影、序号和哈希链事件、
  checkpoint、replay、锁。
- 新增操作者状态：`active | paused | stopped`；`complete | incomplete | repair_required` 每次
  从 TaskGraph、集成 HEAD 和证据派生，不提供写 completed 的 API。
- 增加合约 drift 检测与 `reconcile` revision；reconcile 不重置累计预算。
- start 或 reconcile 固定 targets、依赖闭包和闭包内 Task contract 哈希；正常 status 或 evidence
  变化不触发 drift。
- 在 workspace Objective 锁下维护 mutation scope ownership；observe-only 可以重叠，拥有
  mutation 权限的 active Objective 不能覆盖同一 Task 或依赖闭包。
- ownership 是从 accepted scopes 与未解决 actions 可重建的 projection；uncertain 未解决时
  不释放 ownership。明确 `.dyro/objectives.lock` 与 projection 路径。
- 新增统一 resolver：显式 selector → 当前目录 Profile → 登记的默认或唯一 workspace →
  TTY 选择；非 TTY 歧义必须显式选择。
- CLI：`objective start/list/status/pause/resume/stop/reconcile/scope add/scope remove`。
- 同一 PR 加入过渡期 fail-closed guard：一旦 active Objective 持有 mutation scope，旧
  `task loop/daemon` 命中该 scope 必须拒绝并提示 plan-only Objective 命令；在 PR-08 journal
  完成前绝不尝试委托。显式单 Task 命令仍保留人工控制权。
- `objective start` 交互选择当前 line 和 targets；非交互要求显式参数与 `--yes`。
- line 解析为显式 line → 当前 line/task worktree → 唯一 line → TTY 选择；多 active Objective
  同样只在 TTY 选择，脚本必须显式传 ID。
- dry-run 展示将创建的合约、路径、目标和默认限制，不创建目录或 recent 状态。

测试：

- create/list/status 的 happy path 与取消零写入。
- 同 ID 并发创建只有一个成功；符号链接、路径穿越和非目录目标拒绝。
- 原子写中断、事件断尾、seq 重复、hash 分叉、checkpoint 回滚 fail-closed。
- pause/resume/stop 与 reserved、started、running、uncertain 的完整转移矩阵；stop 不能伪装
  complete，started/running/uncertain 存在时 reconcile 拒绝。
- 两个进程并发申请重叠依赖闭包只有一个取得 mutation ownership；observe-only 可重叠。
- active mutation scope 内旧 loop/daemon 拒绝；无 Objective 或 scope 外任务保持原行为。
- 从无关目录、多个 workspace、多个 Objective、无 line 和非 TTY 的解析真值表。
- 合约漂移阻止后续 mutation；reconcile 记录旧、新 hash 和 revision。
- 旧 workspace 没有 objectives 时行为不变。

验证：focused tests、全量 unittest、ruff、`git diff --check`；用临时 workspace 手工完成
一次 create、pause、resume、reconcile 和 dry-run 零写入检查。

出口条件：任意重启后可准确读取 Objective；损坏不被自动覆盖；Objective 尚不能执行 Task；
旧无人值守命令没有绕过 ownership 的中间发布窗口。

回滚：旧版本忽略 `.dyro/objectives`；回滚代码不删除用户 Objective 文件。

### PR-04：共享 action 调度快照、纯计划器与组合图

依赖：PR-02、PR-03。

目标：把现有 Task 调度和 Objective 调度收敛到同一确定性内核；只读。

冷启动先读：

- `src/dyro/graph.py`
- `src/dyro/tasks.py` 的 check_dispatchable、plan_tasks、select_task_wave、集成检查
- `tests/test_graph.py`
- `tests/test_scheduler_snapshot.py`
- PR-03 Objective store API

主要改动：

- 新增 `continuation/snapshot.py`，显式注入 clock，一次读取 graph、status、decisions、
  claims、attempt 摘要、集成状态、Objective revision。
- 新增 `continuation/planner.py`，纯函数为 execute、review、merge、ask、Trigger、wait 和
  derived complete 生成 selected、blocked、attention 和 next wake。
- 从同一 snapshot 与 plan 生成不可变 `SchedulerReadProjection`；使用稳定 code 和类型化事实，
  不包含本地化文本、路径、argv、日志或 mutation callback。
- 将 `tasks.py` 的 readiness 逻辑抽成共享 snapshot/planner primitive；保持原函数签名的
  兼容 wrapper。
- Task 状态、依赖集成、decision、claim、conflict group 只计算一次。
- 删除 daemon 独立扫描 review 状态的 readiness 分支；旧命令只消费 action-level plan。
- 新增 `objective plan/explain/graph` 以及 text、JSON、Mermaid 输出。
- 组合图只投影 Task、Decision、Trigger、Action；constraint 不伪装 dependency。

测试：

- 固定 clock 下相同输入的 canonical plan 字节一致。
- 现有 `task next/loop/daemon` 的 execute/review 行为与 PR-01 后 golden fixtures 一致，且共享
  同一 reason-code 表；merge 仍不由旧 review queue 隐式启动。
- 依赖未 done、done 未集成、决策 open、claim active、conflict active 的 reason code。
- Objective targets 的依赖闭包和跨 line 拒绝。
- JSON 与 Mermaid 使用同一节点和边集合。
- plan、explain、graph 不写状态、不启动 Agent、不探测网络。
- 1000 Task 合成图基准记录，避免 O(N²) 状态扫描。

出口条件：execute、review、merge、ask、Trigger、wait 和 complete 只存在一个 action
readiness 算法；所有旧调度测试按 PR-01 新边界无意外回归；Objective 可完整解释但不能执行。

回滚：兼容 wrapper 允许整 PR 回滚，不迁移 Task 文件。

### PR-05：纯预算计算、共享额度与无进展停止

依赖：PR-04。可与 PR-06 并行。

文件所有权：`continuation/budgets.py`、对应测试；不修改 trigger/action 实现。

目标：用纯函数计算任一计划的有效额度和最坏情况 reservation；不在本 PR 持久化 reservation。

主要改动：

- 实现 workspace 上限、Objective 请求、本地 activation 三者交集。
- 实现 action、每 Task attempt、失败、连续失败、并行度、deadline、provider usage。
- Snapshot 纳入 workspace 内所有 Objective 的 active reservation，强制共享并发与共享额度池，
  防止两个不重叠 Objective 分别满足局部上限却突破 workspace 上限。
- 输出 `BudgetDecision`、reservation amount 和拒绝 reason；持久化 reserve/commit/release 由
  PR-07 以 ActionIntent/Receipt 完成。
- 实现 progress fingerprint 与 no-progress 纯计算，只覆盖 Task、集成 HEAD、decision 和有效
  evidence；Trigger 变化不重置。
- hard provider usage 无可信 receipt 时拒绝自动化；Core 可强制的本地限制始终生效。

测试：边界值、workspace 聚合并发、过量拒绝、deadline、UTC、时钟回拨、Trigger 抖动不
reset、真实交付进展 reset，以及同一 snapshot 的 BudgetDecision 确定性。

出口条件：纯预算真值表和 workspace 聚合守恒通过；模块不读写文件。

回滚：预算模块尚未接入 mutation，整 PR 可回滚。

### PR-06：Trigger 协议、内置观察器与退避

依赖：PR-04。可与 PR-05 并行。

文件所有权：`continuation/triggers.py`、provider 边界、对应测试。

目标：低成本判断何时重新规划，且 Trigger 永远没有交付 mutation 权限。

主要改动：

- 内置 `time_due`、`task_state`、`decision_state`、`manual_signal`、`local_ref`。
- 定义可审计 provider 描述和有界 JSON 子进程协议；不把第三方 Python 导入 Core 进程。
- provider 加载设 allowlist、schema、输出大小、超时、错误分类和机密守卫。
- 实现确定性 jitter、指数退避、最大间隔、状态变化立即唤醒。
- Trigger satisfaction 只请求 replan 或生成 attention，不释放 TaskGraph dependency、decision、
  gate、review 或 evidence requirement；`not_before` 使用 Objective 时间策略。
- CLI：`trigger list/probe/signal`；probe 的 dry-run 只验证配置，不访问外部资源。
- 不在 Core 提供任意 HTTP URL 或 shell Trigger。

测试：时间边界、sleep/wake、未变化退避、变化唤醒、临时或永久错误、认证缺失、恶意
provider 输出、超大结果、重复观测去噪、外部模式边界，以及 observation 不能改变 Task。

出口条件：Trigger 只能产生 observation 和 next wake；协议不暴露 Task mutation API。安装在
同一用户权限下的第三方程序仍按受信任本机软件处理，不宣称操作系统隔离。

回滚：删除 Trigger 配置只让 Objective 回到普通 Task 状态驱动，不影响 TaskGraph。

### PR-07：Action journal、fencing、启动屏障与崩溃恢复

依赖：PR-03、PR-04、PR-05。

文件所有权：`continuation/actions.py`、`continuation/leases.py`、managed-process 模块、必要的
`state.py` 和 `process.py` 扩展。

目标：所有未来副作用都有写前 intent，且崩溃后不会盲目重复执行。

主要改动：

- 不可变 ActionIntent、ActionReceipt 和确定性 idempotency key；key 绑定 Objective revision、
  operation、subject 和预期 attempt 或 operation generation，使同一副作用去重而合法重试换代。
- 定义 create-only intent → action-start → process-start → receipt；action-start 是任何副作用
  之前的线性化点，没有 action-start 的 intent 才能取消。
- Objective owner lease：owner token、单调 fencing generation、PID、进程启动标识、heartbeat、
  deadline；每次取得或接管递增 generation，CAS 续租或释放必须匹配 token 和 generation。
- intent、action-start、process-start、receipt 和 budget 都绑定 generation、accepted revision、
  scope manifest 与 policy digest。
- 在 intent → action-start 前于 workspace Objective 锁下重验 operator state、scope、policy、
  owner、预算和监督确认或 activation；撤权取消未 start intent。
- 新增 launch barrier：受控启动器先持久化 PID/start generation/process group/deadline，再放行
  目标 argv；支持有界输出、timeout 后终止进程树和重启诊断。自动模式尚不启用。
- 定义跨平台 ManagedProcessBackend 自检；POSIX 控制管道/process group 与 Windows 受控事件/
  Job Object 分别验证。当前平台不能证明启动顺序或进程树终止时，Objective mutation 降级为
  plan-only。
- 恢复检查：无 action-start 可取消；有 start 且完整 receipt 可补投影；有 start 无可信终态
  一律 uncertain，不根据 lease 过期推断副作用停止。
- create-only intent 同时作为 budget reservation；事件或投影发布失败时先恢复该 intent，禁止
  继续分配，避免跨文件伪原子事务。
- `objective repair --check` 只读诊断；mutation repair 只允许枚举动作并需 `--yes`。
- 禁止根据 lease 到期推断 Agent 副作用已停止。

测试：intent、action-start、launch barrier、process-start、receipt 每个边界的 fault injection；
双进程 owner、PID 复用、过期接管、旧 owner 发动作或释放、pause 位于 intent 与 start 之间、
receipt 重放、intent 冲突、输出上限、timeout 进程树、uncertain 永不自动重试、symlink 与
create-only 攻击。

出口条件：crash matrix 和 fencing matrix 全绿；目标 argv 不可能早于 durable process-start
运行；不存在“无法证明却继续执行”的恢复路径。

回滚：尚未编排实际 Task mutation；保留 journal 供诊断，旧版本忽略。

### PR-08：监督式 engine、`dyro continue` 与现有执行链集成

依赖：PR-06、PR-07。

目标：完成第一个可用闭环，但每个 delivery mutation 仍由用户确认。

冷启动先读：

- `tasks.py` 的 run_task、answer_task、review_task、merge_task
- `provenance.py` 的 attempt begin 或 finish、review binding
- `process.py` 的进程生命周期
- PR-04 planner、PR-05 budgets、PR-07 actions

主要改动：

- 新增 `continuation/engine.py`：lock、snapshot、plan、authority check、reservation、intent、
  existing API、receipt、replan。
- CLI：`objective tick` 和顶层 `continue`。
- `continue` 每次最多应用一个 wave，先显示 Task、Agent、worktree、操作、预算和不能执行的
  边界；监督模式要求确认。
- 为 ExecutionAttempt 增加可选 Objective 或 action binding，保持 schema 1 读取兼容或按
  明确迁移规则升级。
- 为 run_task、gate 和 review 注入 PR-07 managed-process context；显式旧命令使用兼容默认值。
- 只接入 execute 和 review；merge 仅显示 attention，push 始终拒绝。
- review receipt 结束当前 action；任何 merge 都只能由下一次 snapshot 生成独立 action。
- external mode 只规划、probe 和提示控制面显式导入证据；engine 不调用 evidence import。
- 让 `task next/loop/daemon` 调用共享 scheduler primitive，删除重复 readiness 分支。显式单
  Task 命令保留人工控制权；把 PR-03 对 active mutation scope 的旧 loop/daemon 拒绝升级为
  委托同一 action journal，不能绕过预算。

测试：fake Agent 的 DONE、QUESTION、failure、timeout；review PASS 或 FAIL；review 绝不
merge/push；answer 后新 attempt；budget reservation/settlement；确认取消；dry-run 零写入；
process crash 到 uncertain；external mode 不启动本地动作或导入证据；旧 CLI 输出兼容。

人工验证：在隔离 fixture workspace 完成 execute → question → answer → execute → review，
杀死一次执行进程并确认不重复运行。

出口条件：监督式闭环可用；所有质量门仍由现有 API 证明；没有无人值守 mutation。

回滚：Objective 退回 plan-only；Task attempt 和证据仍由旧命令读取。

### PR-09：Attention 投影与零摩擦 Home

依赖：PR-08。

目标：让新用户从任意目录理解并推进当前目标，不需要学习内部术语或编辑 TOML。

主要改动：

- 新增 `continuation/attention.py` 和 `dyro attention`。
- 暴露不可变 `AttentionReadProjection`，让 Home、结构化 CLI 和其他只读展示面消费同一排序、
  reason code 与安全事实，不允许展示面反向修改 Objective 或 Task。
- bare `dyro` 在 workspace 选择后先显示 active Objective 摘要与唯一推荐动作。
- 首页分组：repair、needs you、ready、paused、waiting；使用稳定排序。
- “继续目标”走 Task 合约 Agent；“打开工作区”保留现有 coding-tool picker。
- Objective 最近使用记录写入独立、带版本的全局偏好文件，不向严格 v1 workspace registry
  增加字段；它只影响展示，不增加权限。
- 每个错误包含原因、影响和一个恢复命令；非 TTY 输出稳定且可脚本解析。

测试：无 Objective 完全兼容；单或多 Objective；stale workspace；缺 Agent；问题、决策、
预算、uncertain；首页取消零写入；从任意目录选择 recent workspace；旧版本仍能读取未改变的
workspace registry；编码工具选择流程不回归。

可用性验收：一名不了解 Dyro 内部名词的用户，在三分钟内完成创建 Objective、读懂下一步、
进入监督确认；过程中不编辑 TOML。

出口条件：首页没有把 attention 当作交付状态；原有工具选择、安装引导和 workspace 打开流程全绿。

回滚：关闭 Objective 首页卡片并忽略独立偏好文件即可恢复 v0.5.6 Home；workspace registry
格式未变，Objective 状态不丢失。

### PR-10：限时自动执行、复核与 daemon

依赖：PR-09。

目标：在明确授权和预算内跨时间继续，同时默认保持关闭。

主要改动：

- `objective activate --mode automatic --until ... --yes`；ActivationLease 绑定 workspace、
  Objective accepted revision、scope manifest、contract hash、workspace policy digest、operation
  allowlist、approved Objective budget hash、可选且只会收紧的 budget limits、actor、本机
  installation ID、activation generation 和 expiry，reconcile 后失效。
- `activation.json` 使用 0600；实现 issue、renew、revoke、expire、时钟回拨失效和跨机器复制
  降级。全局 installation ID 独立版本化并使用 0600。
- `objective daemon` 前台模式；消费 host-neutral `next_wake_at`，不绑定系统服务管理器。
- Trigger due、Task 变化或 signal 唤醒；无变化时退避，不 busy-loop。
- conflict-aware 并行 wave、per-Agent limit、Objective owner heartbeat。
- enforce 单一 mutation ownership；多个 observe Objective 可重叠，多个写 Objective 不可覆盖
  同一 targets 或依赖闭包。
- 接入自动 execute 和 review；merge 在本 PR 始终只生成 attention。
- 明确拒绝自动 push，即使 Task 中存在 push 请求也只生成 needs_you。
- lease 到期、budget、no-progress、uncertain、dirty、contract drift 立即降级并停止新动作。

测试：双 daemon、fencing takeover、lease expiry、review 中 lease 到期后不能链式启动 merge、
SIGTERM、sleep/wake、时钟回拨、跨机器 installation ID、Trigger backoff、并发 conflict、两个
Objective 的 workspace 共享并发、外部模式、自动 push 全路径拒绝。PR 必跑 soak 使用虚拟时钟
与故障注入。

安全评审：单独检查权限交集、TOCTOU、process tree、journal replay、插件边界和日志机密。

出口条件：默认安装不启动 daemon；自动模式只能由显式、限时、同机授权开启；停止条件在
action-start 前生效；本 PR 没有自动 merge 路径。

回滚：撤销 ActivationLease 或关闭 workspace 上限即可即时停发新动作；不删除已完成证据。

### PR-11：独立授权的自动本地 merge

依赖：PR-10。

目标：把本地 merge 作为与 review 完全分离的 action 接入；自动 push 仍不存在。

冷启动先读：

- PR-01 review/merge 分离不变量
- `tasks.py` 的 merge preflight、事务恢复、review/signoff binding 复验
- PR-04 action planner、PR-07 fencing、PR-10 ActivationLease

主要改动：

- 只有 workspace `allow_unattended_merge`、Objective operations 包含 merge、Task
  `merge.auto=true`、同机 ActivationLease 有效、owner fencing 有效、当前 review/signoff 与
  task HEAD 完整绑定时，planner 才选择 `merge_task`。
- merge 使用独立 intent、action-start 和 receipt，并硬编码 `push=False`；Task 中的 push 请求
  只生成 `needs_you`，不能传入自动 action。
- action-start 前重跑所有权限和 binding 检查；进入现有 merge lock 后由 `merge_task` 再做
  全仓 preflight。review receipt 不能携带或触发 merge。
- merge 期间 lease 到期允许当前事务按现有恢复协议结束，但禁止任何后续 action；无法证明
  终态则 uncertain 并保留 scope ownership。

测试：三层授权与 activation/fencing 的完整真值表；任一权限缺失都只提示；review 中 lease
到期不 merge；review 之后新 tick 才可 merge；task HEAD 或 signoff 漂移拒绝；多仓预检和中途
失败恢复；所有自动路径 mock Git push 并断言零调用。

出口条件：自动 merge 可独立关闭和回滚；review、merge、push 三者在 plan、intent、ledger、
receipt 和测试中完全分离；自动 push 零路径。

回滚：关闭 workspace `allow_unattended_merge` 即停止新 merge；已开始事务仍按现有 merge
恢复协议收敛，不回滚已证明成功的 Task 证据。

### PR-12：0.6.0 统一迁移、压力、安全、产物与发布门禁

依赖：PR-11、Console PR-C06。

目标：把续航与 Console 两条已完成的实现线从“功能完成”提升为可共同公开发布的
`0.6.0` 版本候选。PR-12 不承接 Console 的首次安全实现；发现 Console 缺口必须回到
C01–C06 的对应 owner 修正并重新通过 C06。

主要改动：

- 更新 architecture、README 多语言命令表、迁移指南、故障排查、示例 Objective。
- 将 PR-01 terminology-policy scanner 扩展到生成帮助、sdist、wheel 和发布元数据，并校验
  policy input hash 与批准记录一致。
- 增加升级或降级、1000 Task、100 Objective、10k event replay、24 小时 soak fixtures。
- 增加 wheel 或 sdist 源码树外安装测试，检查 package data、CLI help、entry points。
- 对 Objective 状态路径运行 symlink、权限、截断、并发和磁盘满故障演练。
- 汇总 Console C06 的 localhost、只读、脱敏、浏览器、可访问性、性能和 clean-install
  证据，并复核其 `WorkspaceReadSnapshot` 只消费 Core 投影。
- 完成独立架构、安全、静默失败和测试覆盖评审；P0 或 P1 清零。
- 准备 CHANGELOG 与版本候选，但 tag、Release、PyPI 仍走单独发布授权。

完整门禁：

```bash
uv lock --check
uv run --frozen python -m unittest discover -s tests
uv run --frozen ruff check .
uv run --frozen python -m compileall -q src tests
uv build
uv run --frozen python -m twine check dist/*
git diff --check
```

另在 checkout 外新建临时虚拟环境，分别安装 wheel 和 sdist，运行 `dyro --help`、Objective
smoke、旧 Task smoke、entry points、包资源和 wheel/sdist 文件清单检查。使用仓库外 deny list
对工作树、暂存 diff、分支名、提交标题、生成帮助和构建产物元数据执行术语策略扫描，结果
必须为零命中。PR CI 使用虚拟时钟 soak；发布前还必须有一轮独立的真实 24 小时定时任务绿灯。

出口条件：全量 CI 绿、所有 P0 或 P1 已解决、续航与 Console 的源码树外安装均通过、迁移或
降级通过、Console 仍无浏览器 mutation、自动运行默认关闭、无自动 push、术语策略零命中；
之后才能进入 `0.6.0` 的版本号、tag、Release、PyPI 独立发布流程。

回滚：发布前直接回滚 PR；发布后用 workspace policy 关闭 continuation mutation，旧 Task
命令继续可用，Objective 文件保留供未来升级读取。

## 6. 阶段里程碑

| 里程碑 | 包含 PR | 用户可见结果 | 默认权限 |
| --- | --- | --- | --- |
| M0 权限基线 | 01 | review/merge 分离，external reviewer 独立 | 显式控制面 |
| M1 可解释 | 02–04 | 创建目标、plan、explain、graph | 只读 |
| M2 可监督 | 05–08 | budget、Trigger、恢复、`dyro continue` | 每 wave 确认 |
| M3 易上手 | 09 | bare `dyro` 目标首页与 attention | 每 wave 确认 |
| M4 可续航 | 10 | 限时 daemon、自动 execute/review | 默认关闭，同机显式租约 |
| M5 可合并 | 11 | 独立授权的自动本地 merge | 默认关闭，永不自动 push |
| M6 统一可发布 | 12 + C01–C06 | 续航与只读 Console 的迁移、安全、产物、文档、发布候选 | 自动运行默认关闭，仍不自动 push |

单一有经验工程师的粗略工作量为续航 42–55 个工程日，加 Console 18–26 个工程日。PR-09
之后，PR-10/11 与 Console C01–C06 可并行；双人排期应以共享契约冻结与 PR-12 统一门禁为
准，不能以并行名义跳过任一安全出口条件。

## 7. 全局验收矩阵

| 维度 | 必须证明 |
| --- | --- |
| 正确性 | 同一快照同一计划；不存在越权依赖、decision、conflict、budget |
| 唯一事实源 | TaskGraph 与现有证据仍唯一；Objective 不复制 Task |
| 恢复 | receipt 可续；complete 实时派生；uncertain 停机；无静默重复执行 |
| 权限 | 有效权限是四层交集；external mode 只观察；push 永不自动 |
| 易用性 | 从任意目录开始；无 TOML 编辑；一个推荐动作；错误可恢复 |
| 兼容性 | 无 Objective 的 workspace 保持；仅 PR-01 明示的危险旧行为收紧并提供迁移 |
| 性能 | 大图一次读取；无 O(N²) 状态扫描；等待不 busy-loop |
| 隐私 | 本地优先、无默认遥测、无凭据落盘、provider 输出有界 |
| 产物 | wheel 或 sdist 在 checkout 外完整可用 |
| 开源卫生 | 源码、文档、示例、历史增量和构建元数据通过术语策略 |

## 8. 每个 PR 的统一执行协议

1. 记录源分支、精确 HEAD、dirty 状态和目标 worktree 路径。
2. 创建独立 branch 或 linked worktree；不切换用户当前 checkout。
3. 先跑依赖 PR 的基线测试。
4. 只修改本 PR 文件所有权范围；发现前置接口缺陷时先停下修正依赖。
5. 运行 focused tests、全量 unittest、ruff、diff check。
6. 由独立 reviewer 做架构、安全、静默失败和测试覆盖检查。
7. 术语策略扫描工作树、diff、分支和提交候选。
8. 展示 diff、测试证据、剩余风险；单独取得 commit 授权。
9. commit 后复跑关键门禁；单独取得 push 或 PR 授权。
10. PR 合并后记录 merge SHA，下一步只从该 SHA 开始。

## 9. 本轮设计文档落库范围

设计批准后，先创建一个仅文档的独立 worktree 和分支，作为不计入上述实现 PR 的
设计记录 PR，落库：

- `docs/adr/0004-native-continuation-engine.md`
- `docs/designs/native-continuation-engine.md`
- `plans/native-continuation-engine-implementation.md`
- `docs/adr/0005-local-web-console.md`
- `docs/designs/local-web-console.md`
- `plans/local-web-console-implementation.md`

该文档 PR 不修改运行时代码、版本号、CHANGELOG，不 commit、不 push，直到分别获得对应授权。
落库前使用仓库外 deny list 扫描六份文档、目标路径、分支名和提交标题候选，并记录 policy
input hash 与零命中结果；不得等到 PR-12 才开始执行。
