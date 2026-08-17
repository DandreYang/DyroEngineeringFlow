# Dyro Console 本地 Web 控制台设计

状态：提案
目标版本：0.6.0；与持久续航引擎同一发布列车
适用范围：全局工作区、TaskGraph、Objective、Attention、证据元数据与本机健康状态

## 1. 产品定位

Dyro Console 是 **Dyro 权威状态的本地只读窗口**。它解决的不是“再提供一套操作系统”，而是让使用者在一个页面中快速回答：

- 我登记了哪些工程，哪些当前可用？
- 哪些开发线、Objective 和 Task 正在推进、等待或需要修复？
- 每个阻塞来自依赖、决策、证据、预算、工具还是工作区健康？
- 当前最安全的一步是什么，应该回到哪条 CLI 命令处理？
- 最近一次执行、复核、签收和集成分别处于什么阶段？

首版只读是一项产品约束，不是临时缺功能。浏览器最擅长汇总、筛选、关联和解释；真正的执行、复核、签收、合并、推送、安装与升级仍进入现有 CLI 权限、确认、锁、证据和审计路径。

## 2. 用户与关键旅程

### 2.1 新用户：三十秒看懂

```bash
dyro console
```

命令从任意目录启动本地服务并打开浏览器。页面首先显示：

1. 当前或默认工作区；
2. “需要你处理”的事项；
3. 正在运行、可推进和等待的任务数量；
4. 一条用自然语言说明的推荐动作；
5. 可复制的恢复或推进命令。

用户不需要先理解 Profile、claim、receipt、revision、fencing 或 evidence generation。高级概念只在详情中按需展开。

### 2.2 多项目负责人：先看异常，再看进度

全局概览按以下优先级排序工作区：

1. `repair_required`；
2. `needs_you`；
3. 有 active Objective 或运行中 Task；
4. `paused`；
5. `waiting`；
6. 无活动且健康。

排序使用稳定 reason code 和事实字段，不依赖翻译后的文案。一个工作区不可用时保留卡片和恢复入口，不从列表中静默消失。

### 2.3 工程师：从全局状态下钻到证据

工程师可以按工作区 → 开发线或 Objective → Task 下钻，查看：

- 依赖与决策阻塞；
- 当前 Task 状态与是否已集成；
- executor、reviewer 与签收要求；
- attempt ID、结果、开始或结束时间和绑定哈希；
- gate 通过或失败摘要；
- review、sign-off、merge 的派生状态；
- 最近事件与下一次有意义的唤醒时间。

页面不展示 prompt、answer 正文、argv、环境变量、完整日志、远程地址或未经验证的 provider 输出。

## 3. 信息架构与页面契约

### 3.1 全局框架

桌面端左侧为工作区导航，移动端折叠为顶部选择器；主区域包含全局搜索、最后刷新时间、局部失败提示和页面内容。搜索只过滤已经加载的安全字段，不把输入发送到外部服务。

一级页面：

| 页面 | 首要问题 | 默认内容 |
| --- | --- | --- |
| 全局概览 | 哪些项目最需要关注？ | Attention、active work、健康与任务计数 |
| 工作区 | 这个工程整体发生了什么？ | 开发线、Objectives、任务状态分布、健康 |
| Objective | 为什么继续、等待或暂停？ | 预算、Trigger、下一唤醒、action 与 attention |
| Task | 这项工作凭什么处于当前状态？ | 依赖、attempt、gate、review、sign-off、集成 |
| 任务图 | 工作如何依赖与并行？ | 图形视图和等价表格视图 |
| 活动 | 最近发生了什么？ | 白名单化事件时间线，游标分页 |
| 本机状态 | Dyro 和编码工具是否就绪？ | 已缓存版本信息、工具状态与恢复命令 |

### 3.2 概览卡片

每张工作区卡片必须在不展开的情况下展示：

- 别名与 Profile 名称；
- `healthy | degraded | unavailable` 健康标签；
- active Objective、运行中 Task、`needs_you`、`repair_required` 数量；
- 当前最高优先级事项和一条推荐动作；
- 数据捕获时间以及 `fresh | stale | partial` 标记。

“健康”只表示读取和结构检查没有发现问题，不表示交付完成。“无活动”也不能显示成“已完成”。

### 3.3 工作区详情

工作区详情按“需要关注 → 正在推进 → 其余状态”排序，而不是先展示仓库内部结构。首屏关注列表只投影同一次 summary 快照里的 Objective attention，并去掉 `PROOF_DECAYED`。空列表写成「摘要未列出关注项」；工作区不可读时写成未知，不得按 0 或「没有事项」展示。开发线表格提供 Task 分布、dirty 或 missing 摘要和最近活动。仓库 Git 深度检查异步加载，不阻塞页面基本信息。

绝对根路径和 remote URL 默认不进入 API。所有恢复命令优先使用安全别名，例如：

```text
dyro --workspace example doctor
dyro --workspace example objective tick release-readiness
dyro --workspace example objective attention release-readiness
dyro --workspace example task open API-101
```

### 3.4 Objective 详情

Objective 页面同时显示两个互不混淆的维度：

- 操作者状态：`active | paused | stopped`；
- 派生结果：`incomplete | complete | repair_required`。

页面展示 accepted revision、scope 摘要、运行模式、有效权限交集、预算余额、no-progress 次数、activation 到期时间、Trigger 状态、`next_wake_at`、当前或最近 action 及其 receipt。任何 `uncertain` action 固定置顶，并明确说明 Console 不能代替 repair。

### 3.5 Task 详情

Task 详情按证据链排列：

```text
Task contract
  → scheduler explanation
  → execution attempt
  → gates and task HEADs
  → independent review
  → optional sign-off
  → integration / merge state
```

哈希默认显示前 12 位，可显式复制完整值。复制只发生在浏览器内，不调用服务端 mutation。完整日志、handoff 正文和私密回答不在首版接口中。

### 3.6 图视图

图节点和边直接来自共享组合图投影：Task、Decision、Trigger、Action 为节点；依赖和阻塞为边；conflict group、budget 与 lease 是约束。图视图不得推导新的 readiness。

- 默认按开发线和状态过滤，避免一次绘制全部节点；
- SVG 视图支持缩放、平移、键盘选择和减少动画；
- 同一页面始终提供等价的节点或边表格；
- 图超过 250 个可见节点时默认切换为过滤后的局部视图；
- 图结构无效时展示 Core issue，不尝试“修好后继续绘制”。

## 4. 权威边界与不变量

```text
Browser
   │ authenticated GET
   ▼
Loopback HTTP boundary
   │ typed request
   ▼
Console DTO whitelist
   │ capture ID
   ▼
Core WorkspaceReadSnapshot
   ├─ global workspace registry
   ├─ shared SchedulerSnapshot / ContinuationSnapshot
   ├─ TaskGraph and scheduler explanation
   ├─ Attention projection
   ├─ evidence and attempt metadata
   └─ cached tool / update state
```

必须始终成立：

1. TaskGraph、Task 状态、Objective journal、action receipt 和证据仍是唯一事实源。
2. Console 只投影，不保存 Task、Objective、attention 或“完成”副本。
3. `ContinuationSnapshot` 是 Core 内部模型；只经过显式字段白名单后进入 `ConsoleReadModel`。
4. Console 不通过 subprocess 调用 `dyro ... --format json`，也不解析人类 CLI 输出。
5. 页面交互不能释放依赖、满足 gate、接受 review、签收、集成或改变预算。
6. 启动和读取 Console 不调用 `mark_workspace_used`，不更新 recent preference，不追加 ledger。
7. 更新卡片只读取 `UpdateState` 缓存；页面刷新不调用 PyPI 或任何远程地址。命令启动阶段的每日检查仍只由现有 CLI 统一策略决定。
8. 外部运行器或 provider 的展示数据不能扩大其在 Core 中的权限。

Core 读取层在一次 capture 中生成不可变 `WorkspaceReadSnapshot`，包含 `capture_id`、
`workspace_revision`、`observed_at`、`source_digests`、`completeness`、结构化组件错误和安全事实。
overview、workspace、Objective、Task、graph 与 activity endpoint 只能切片或进一步脱敏这份快照，
不得各自重算 readiness、attention、completion、evidence validity 或 health。读取不能触发 lazy
index、偏好更新、Git 锁写入或其他 hydration mutation。

### 4.1 C01 已落地的读取契约

首个实现仅建立 Core capture 与 Console DTO 边界，尚未启动 HTTP listener 或浏览器：

- `dyro.observations.capture_workspace_read_snapshot()` 只组合既有 TaskGraph、Objective、
  scheduler 和 Attention 读取；它不调用编码工具、网络、更新检查、CLI 子进程或任何 mutation API；
- 读取失败收敛为稳定的组件 code，例如 `TASKS_UNAVAILABLE` 与
  `OBJECTIVES_UNAVAILABLE`，不返回异常文字、路径或解析原文；一个 Objective 组件失败不得抹掉
  已捕获的任务和开发线；
- `dyro.console.workspace_envelope()` 只接受该不可变 snapshot，逐字段白名单化后生成统一封套；
  `snapshot_sha256` 只覆盖脱敏后的事实，不覆盖 `captured_at`，因此相同状态的轮询可复用 ETag；
- C01 不发布 action receipt、ledger、gate 输出、agent argv、adapter 环境、仓库路径或 remote。
  后续阶段若要扩充字段，必须先扩充显式 DTO 与脱敏测试，而不能把 Core dataclass 直接 JSON 化。

### 4.2 C02 已落地的 listener 契约

C02 只提供无项目数据的静态 shell、一次性 session 交换和认证后的 `/api/v1/meta`：

- `create_console_http_server()` 没有 host 参数，只能绑定 `127.0.0.1`；它对 request line、
  headers、body、并发数、读取时间和单请求总时限设置固定上限；
- 每个请求必须使用精确的 `Host: 127.0.0.1:<actual-port>`；转发 header、`Transfer-Encoding`、
  非 origin-form target 与不允许的方法都 fail closed，且不会启用 CORS 或访问日志；
- bootstrap 仅在精确 same-origin JSON POST 中使用一次，随后换发独立的内存 bearer。session 没有
  cookie，使用 30 分钟 idle 与 8 小时 absolute 上限；退出 server 时会清空全部 session；
- C02 没有 CLI `console` 命令、浏览器打开、资源文件读取或 workspace API。它的唯一目的，是在
  接入真实 read model 前先固定并测试本地 HTTP 安全边界。

### 4.3 C03 已落地的全局概览契约

C03 在 listener 外新增 `ConsoleOverviewService`，把既有 registry 与每个工作区的一次
`WorkspaceReadSnapshot` 投影为只读、分页的 overview：

- listener 只验证 bearer、参数和 ETag；它不拥有 registry、Config 或 workspace 路径。概览服务
  是可注入的读取边界，后续 worker 化不会改变 HTTP DTO；
- 工作区按 `repair_required`、`needs_user`、active、paused、waiting、其余健康项目的固定优先级
  排序。每张卡片只含 alias、已净化展示名、计数、attention、一个安全 CLI 恢复建议和摘要 digest；
- registry 读取失败收敛为 `REGISTRY_UNAVAILABLE`。单个 Profile 或 snapshot 失败保留
  `unavailable` 卡片和 `WORKSPACE_UNAVAILABLE`，不会影响其它工作区，也不返回根路径或原始错误；
- `GET /api/v1/overview` 需要 bearer，`limit` 最大 100。下一页 cursor 与完整的当前概览 digest
  以进程内 256-bit key 认证；篡改或状态变化后的 cursor 返回稳定错误，绝不退化为不透明 offset；
- overview 的 `snapshot_sha256` 可作为 ETag 使用；同一脱敏页面带精确 `If-None-Match` 时返回
  304。C03 仍没有 CLI、浏览器、静态资源或详情 API。

### 4.4 C04 已落地的 inspection 与工作区摘要契约

C04 将真实工作区读取移出 HTTP 请求线程，并补齐概览的单工作区下钻入口：

- `create_console_http_server()` 默认装配 `IsolatedOverviewService` IPC client。它以固定 `python -m`
  argv、最小环境和新 session 启动 inspection process；worker 不继承 bearer、bootstrap、编码工具
  配置或其它宿主环境值，异常 stderr 不会进入 API；
- inspection outer worker 对单次请求施加 8 秒硬 deadline。它在内部最多运行 4 个 daemon 子进程，
  每个 workspace 3 秒、整页内部采集最多 6 秒；超时或崩溃只返回该 workspace 的
  `unavailable`/`WORKSPACE_TIMEOUT` 卡片，
  并在父 deadline 后终止整个 process group；
- 当前 process-tree 回收仅在 POSIX 平台启用；Windows 在具备经验证的 Job Object 回收实现前对
  inspection fail closed，不会以“只终止 outer process”的方式留下读取子进程；
- worker 到 listener 只返回有大小上限的规范 JSON。父进程重新校验 schema、digest、freshness 和
  payload 形状；无效输出、超时或 worker 失败全部 fail closed 为稳定 code；
- `GET /api/v1/workspaces/{alias}` 需要 bearer，只接收单段安全 alias。响应复用同一
  summary DTO，并附带同一次 summary 快照里已经捕获的 `lines` / `tasks` /
  `objectives`。这不是独立 inspect：摘要卡必须带 `proof_inspection=not_inspected`，
  任务 `integration_state` 必须是 `not_inspected`，不得带 `proofs`，也不得把
  `PROOF_DECAYED` 写进这张详情。未知 alias、编码 traversal 或双段路径不会落入
  workspace 读取；
- `GET /api/v1/workspaces/{alias}/proofs` 是独立 inspect，不改写 summary。摘要卡必须带
  `proof_inspection=not_inspected`；父进程若看到 `inspected` 会拒收该卡。inspect 与
  summary 共用同一 exec worker 与进程组回收；超时由父进程 `killpg`，只回报未检查，
  不把摘要标成已检查。不得在 worker 内再 spawn 一层后成功退出，留下 hung git；
- overview 和 workspace 的 ETag 覆盖 data 以及 `freshness.state`、`partial` 和 warnings，排除仅
  表示采样时刻的 `captured_at`，因此 warning-only 变化也会使条件请求重新获得 200。

### 4.5 C05 已落地的 Console 启动与浏览器交接

C05 提供前台入口 `dyro console [--no-open] [--port PORT]`，但不增加任何浏览器写能力：

- listener 仍只能绑定 `127.0.0.1`，默认端口为 `0`，没有 `--host` 选项；启动器只会把固定的
  Console server factory 与只读 `IsolatedOverviewService` 组合起来；
- listener 就绪后，CLI 仅将一次性 bootstrap secret 放入浏览器 fragment。自动打开成功时，终端
  只显示不含 secret 的 origin；`--no-open` 或浏览器打开失败才打印完整的、单次且 60 秒有效的
  手工 URL；
- `Ctrl-C` 或任意前台返回路径都会关闭 listener，并清空内存 session；`--dry-run` 仅输出
  `127.0.0.1:<port>`、焦点和浏览器计划，绝不 bind、生成 secret、读写 recent state 或打开浏览器；
- `--workspace` 只写入认证后可见的初始焦点，不减少或增加 registry 可读取的工作区；`--root`
  先验证 Profile，再将该 root 作为临时的单 workspace 只读 inspection 目标，不登记到全局 registry；
- bare `dyro` Home 新增“查看全部项目控制台”。它复用当前已登记工作区作为初始焦点，且 Home 的
  dry-run 会保留零副作用；需要不登记某个 Profile 时，应显式使用 `dyro --root PATH console`。

### 4.6 C06 已落地的离线总览界面

C06 将已认证的总览接口接入无框架浏览器界面，优先让使用者在一页内识别需要处理的工程：

- `index.html`、`app.js` 和 `styles.css` 是 wheel 内的固定资源；每个启动都通过 SHA-256 与大小
  manifest 验证，缺失、漂移或未知 asset 使 listener fail closed，绝不从 cwd 或 source tree 回退；
- 页面先将 fragment 中的一次性 bootstrap secret 读入局部变量并立即清除 URL，再交换为只存在
  当前 tab `sessionStorage` 的 bearer。它不用 cookie、`localStorage`、外部资源、inline script、
  `innerHTML` 或服务端 mutation；
- 全局页显示 attention、Task 状态分布、active Objective、健康与 freshness、推荐命令。所有
  workspace 文本通过 DOM `textContent` 写入；复制只在浏览器内进行；
- 可点击卡片查看当前 C04 summary，支持条件 ETag 刷新。页面在后台时暂停轮询，恢复可见时只恢复
  单一轮询定时器；会话过期或本地读取失败显示一条恢复说明，不猜测数据或修改项目。

C06 仅消费已发布的 overview/workspace 只读 DTO。工作区详情可以展示同一次
summary 快照里的线、任务和目标清单；overview 轮询不得带上这些列表。独立 Proof
inspect 只在 meta 声明 `proofs` 能力后由详情页按需请求，不得在 overview 轮询里打开。
单条 line / Objective / Task 证据链、图和活动的深度 API 仍须在对应 Core 投影准备完成后通过单独的只读扩展交付，不能由浏览器自行推导。

## 5. 模块设计

建议模块边界：

```text
src/dyro/observations.py        Core-owned WorkspaceReadSnapshot composition

src/dyro/console/
  models.py          不可变、可版本化的展示 DTO
  read_model.py      snapshot slicing、API DTO 与进一步脱敏
  activity.py        有界、白名单化的时间线读取
  inspection.py      worker protocol、硬 deadline 与进程组回收
  _inspect_worker.py exec 后只调用 Core read snapshot 的内部入口
  redaction.py       路径、错误、事件与文本字段净化
  api.py             路由到 read-model service，不含领域判断
  session.py         一次性 bootstrap 与内存 session
  server.py          loopback、请求上限、安全 header、生命周期
  assets.py          固定 manifest 与安全资源解析
  static/
    index.html
    app.js
    api.js
    router.js
    styles.css
    views/
    components/
    icons/
```

现有模块职责不变：

- `hub.py` 仍是全局 workspace registry 的唯一实现；
- `graph.py` 与续航 planner 仍决定节点、边、readiness 和 reason code；
- `tasks.py`、`provenance.py`、`evidence_store.py` 仍拥有任务和证据读取；
- `updates.py`、`tooling.py` 仍拥有本机更新和工具状态；
- `home.py` 保留命令行首页；只增加进入 Console 的导航选择；
- `cli.py` 只解析 `console` 参数和管理前台进程。

首版使用 Python 标准库 HTTP server 和原生 ES modules，不增加运行时 Web 框架或 Node 依赖。若未来交互复杂度超过这一边界，再通过独立 ADR 评估前端构建链，而不是在功能 PR 中隐式引入。

HTTP 主进程不直接读取 registry 或用户 workspace。它以固定 argv 和净化后的最小环境 `exec`
内部 inspection worker，绝不使用继承主进程 secret 的 `fork` worker。worker 只能调用 Core
read snapshot API，通过有界 pipe 返回规范化 JSON；父进程复验 schema、大小与脱敏标记。worker
不持有 listener、bootstrap secret 或 bearer，超时后整个 process group 被终止。

## 6. 展示读模型

### 6.1 统一响应封套

每个成功 API 响应使用：

```json
{
  "schema_version": 1,
  "captured_at": "2026-10-01T08:00:00Z",
  "snapshot_sha256": "...",
  "freshness": {
    "state": "fresh",
    "partial": false,
    "warnings": []
  },
  "data": {}
}
```

- `schema_version` 版本化 API 语义；未知 major 必须拒绝。
- `snapshot_sha256` 对规范化、已脱敏的 `data`、workspace revision、completeness 和结构化错误
  计算；partial 或 stale 不能与 healthy empty data 具有相同 digest。
- `captured_at` 是本次捕获时间，不表示所有工作区具有跨项目事务一致性。
- `freshness.state` 为 `fresh | stale | partial`。
- `warnings` 使用稳定 code、可本地化参数和一条安全恢复命令。

每个 workspace 和 component 另有 `completeness = complete | partial | unavailable`。
Console 不回退到未标记的 last-known-good；如果显示同进程内的旧快照，必须带原始 observed_at、
`stale` 和当前读取错误，不能伪装成最新空数据或 healthy。

错误封套：

```json
{
  "schema_version": 1,
  "error": {
    "code": "WORKSPACE_PROFILE_INVALID",
    "facts": {
      "alias": "example"
    },
    "recovery": {
      "command": "dyro --workspace example doctor"
    }
  }
}
```

客户端根据 `code` 和经过类型约束的 `facts` 本地化说明。服务端异常、绝对路径、traceback、
argv、环境值和原始解析内容不得直接进入 `facts` 或 recovery command。

### 6.2 核心 DTO

`ConsoleOverview`：

- Dyro 版本和 API surfaces（`overview` / `proofs` / `system`；`capabilities` 仍是兼容别名，不是 Capability Card）；
- registry 状态、默认或当前别名；
- `WorkspaceSummary[]`；
- 全局 attention 计数、可读工作区的 Task 状态分布，以及最高优先级事项；
- 缓存更新状态。不可读工作区的任务数不得按 0 计入状态分布。

`WorkspaceSummary`：

- alias、Profile display name、是否默认；
- availability、health、freshness；
- repository、line、Objective、Task 计数；
- Task 状态分布；
- attention 计数和单一推荐动作；
- workspace snapshot digest；
- `proof_inspection`，且只能是 `not_inspected`。独立 inspect 不写进这张卡。

`ConsoleWorkspace`：

- 只读 policy 能力摘要；
- `LineSummary[]`、`ObjectiveSummary[]`、Task 状态分布；
- health finding 摘要；
- 最近活动指针；
- 与 workspace snapshot 绑定的 capture metadata。

`LineSummary` 包含 kind、安全 ID、经过控制字符净化的 branch/base、repository 数量、Task
状态分布，以及按需探测的短 HEAD、dirty 或 missing 摘要。remote URL、绝对 mount 和 Git
命令输出不进入该 DTO。

`ConsoleObjective`：

- contract ID、title、line、accepted revision 和 scope count；
- operator state、derived result、requested 与 effective mode；
- effective authority flags；
- budget used、reserved、remaining；
- Trigger、next wake、progress fingerprint 摘要；
- selected、blocked、attention、active or recent actions；
- snapshot、plan、action 与 receipt 关联 ID。

`ConsoleTask`：

- ID、title、line、risk、状态；
- dispatchable 与稳定 reason code；
- depends_on、blocked_on、conflict group；
- executor、reviewer 和 sign-off requirement；
- attempt metadata、gate result summary、evidence presence；
- review binding、sign-off summary、integrated state；
- 一条由安全 ID 组合出的推荐 CLI 命令。

`ConsoleGraph` 使用 Core 组合图的同一节点、边和 constraint 集合，只增加展示 hint，不改变语义。`ConsoleActivityPage` 只包含事件白名单字段和 opaque cursor。

### 6.3 脱敏白名单

默认允许：

- 安全 ID、用户编写的短 title、状态、reason code；
- 经过长度限制和控制字符净化的 branch、base 与短 HEAD；
- 经过枚举验证的 agent 或 gate 名称；
- 时间、计数、布尔值、耗时；
- attempt、snapshot、plan、receipt 和 evidence hash；
- 由 Core 验证过的相对 artifact 类型，不包含实际绝对路径。

默认拒绝：

- workspace 绝对路径、用户名、remote URL；
- adapter argv、gate argv、环境变量；
- prompt、answer、handoff、receipt 或 review 原文；
- 完整 gate log、stdout、stderr、provider 原始输出；
- credential、token、签名私钥路径；
- 未知 ledger 字段和异常 `repr`。

title 和 branch 是明确的展示字段，但仍受长度、Unicode 控制字符和常见凭据模式净化。使用
显式 `--root` 启动的单 workspace session 可在 recovery command 中返回用户刚刚提供的规范
路径；该命令必须使用平台安全的 argv 展示或 quoting，并在页面标记
`path_disclosure=explicit_root`。全局登记和自动解析路径没有这个例外。

活动时间线按 phase 使用逐类型白名单。未知 phase 只显示时间、Task ID 和 `EVENT_REDACTED`，不得把整个 JSON 原样返回。

## 7. HTTP API

### 7.1 公共资源

| 方法与路径 | 认证 | 行为 |
| --- | --- | --- |
| `GET /` | 否 | 无项目数据的静态 shell |
| `GET /assets/<manifest-name>` | 否 | manifest 内固定静态资源 |
| `POST /api/v1/session` | bootstrap | 一次性交换内存 session |

除上述资源外，所有 `/api/` 路径在认证前都返回统一 401，不通过 404 差异泄露 workspace、Task 或 Objective 是否存在。

### 7.2 只读资源

| 方法与路径 | 返回 |
| --- | --- |
| `GET /api/v1/meta` | 版本、`surfaces`（`overview`、`proofs`、`system`；`capabilities` 为兼容别名）、session expiry |
| `GET /api/v1/overview?cursor=...&limit=...` | 已登记 workspace 的分页轻量摘要 |
| `GET /api/v1/system` | 只读 `updates.json` 缓存。`tools` 恒为空，`tool_inspection=not_inspected` 表示未探测，不得写成没有工具。不探测 PATH，不发起网络检查。5 秒 overview 轮询不拉此接口。 |
| `GET /api/v1/workspaces/{alias}` | 单 workspace 摘要卡，加上同一次 summary 快照的线 / 任务 / 目标清单 |
| `GET /api/v1/workspaces/{alias}/proofs` | 独立 Proof inspect；摘要保持未检查 |
| `GET /api/v1/workspaces/{alias}/lines/{kind}/{line}` | 单 line 或 hotfix 详情 |
| `GET /api/v1/workspaces/{alias}/graph?kind=...&line=...` | 组合图投影 |
| `GET /api/v1/workspaces/{alias}/objectives` | Objective 摘要列表 |
| `GET /api/v1/workspaces/{alias}/objectives/{id}` | Objective 详情 |
| `GET /api/v1/workspaces/{alias}/tasks/{id}` | Task 与证据链摘要 |
| `GET /api/v1/workspaces/{alias}/activity` | 游标分页的安全事件流 |

别名、line、Task 和 Objective 路径参数只解码一次，再使用现有安全 ID 校验。路径中的 encoded slash、NUL、双重编码、`..` 和 Unicode 控制字符全部拒绝。

### 7.3 刷新、分页与缓存

- 每个 GET 接受 `If-None-Match`；ETag 来自脱敏 read model 的 digest。
- 状态未变返回 304；`captured_at` 的自然变化不强制生成新 ETag。
- overview 默认最多返回 100 个工作区；使用 opaque cursor 分页并明确显示总数。
- activity 默认 50 条、最大 100 条；cursor 是带完整性校验的 opaque 值。
- ledger 被截断、替换或 cursor 无效时返回 `ACTIVITY_CURSOR_INVALID`，要求从第一页重新读取，不猜测偏移。
- 普通 JSON 响应上限 2 MiB；graph 上限 4 MiB。超过上限时返回过滤建议而不是截断合法 JSON。
- 活跃页面建议 5 秒轮询，纯等待页面 30 秒；页面不可见时暂停自动轮询，恢复可见后立即条件刷新。

首版不使用 WebSocket 或 SSE。短轮询配合 ETag 已能满足本地状态观察，并减少长连接、重连、背压和退出语义。

## 8. 服务生命周期与 CLI

```text
dyro [--workspace ALIAS | --root PATH] console [--no-open] [--port PORT]
```

- 默认 `--port 0`，让操作系统选择可用端口；不提供 `--host`。
- `--workspace` 只决定初始焦点，不扩大该 workspace 的权限。
- `--root` 以临时只读目标展示一个未登记 Profile，不写 registry。
- 全局 `--dry-run` 只预览 selector、`127.0.0.1:0` 和浏览器动作；不 bind、不生成 secret、
  不打开浏览器，也不读取或写入 recent state。
- listener 就绪后生成至少 256 bit bootstrap secret 和 60 秒有效期。
- 自动打开时，完整 fragment URL 直接交给浏览器，终端只打印不含 secret 的地址。
- `--no-open` 时必须打印一次完整 URL，提示其单次和短时有效；服务日志后续只打印脱敏地址。
- 浏览器打开失败时不退出服务，而是打印同样的一次性 URL 和恢复说明。
- Ctrl-C 优雅停止 listener、等待有界请求结束、清空 session 和内存 cache。
- 无 workspace 时仍打开 onboarding 页面，给出 `dyro setup`、`dyro join` 和 `dyro workspace add` 三条路径。

bare `dyro` 的 Home 在现有工作区和编码工具流程之外增加“查看全部项目控制台”。选择它只启动 Console，不改变当前目标或最近 Agent。

## 9. 本地 HTTP 安全模型

### 9.1 威胁模型

需要防御：

- 任意网站向 loopback 发起跨站请求；
- DNS rebinding 或伪造 Host；
- bootstrap secret 出现在 query、Referer 或 server log；
- 跨站读取 JSON、frame 嵌入、脚本注入；
- URL 路径穿越和静态资源 symlink 逃逸；
- 超大 header、body、慢连接和并发耗尽；
- 工作区中的恶意 title、event 或错误文本触发 DOM 注入；
- API 意外暴露未来新增的内部字段。

不宣称防御同一用户权限下的恶意软件、恶意浏览器扩展、被接管的浏览器，或已经能读取 Dyro
工作区的本机进程；如果未来要把这些主体纳入威胁模型，本地 HTTP 方案必须重新评审，不能
沿用当前 bearer 假设。

### 9.2 Bootstrap 与 session

```text
CLI                         Browser                      Server
 │ generate secret             │                           │
 │ open /#bootstrap=<secret> ─▶│ GET /                     │
 │                             ├──────────────────────────▶│
 │                             │ static shell, no data     │
 │                             │ read secret into memory   │
 │                             │ clear fragment immediately│
 │                             │ POST /api/v1/session      │
 │                             │ secret in bounded body ──▶│ compare + consume
 │                             │◀───────────────────────────┤ independent bearer in JSON
 │                             │ Authorization GETs ──────▶│
```

- secret 使用密码学安全随机源，单次成功后立即销毁；失败尝试有严格上限。
- bootstrap script 先把 fragment secret 读入局部变量，立即用 `history.replaceState` 清除 URL，
  再执行 exchange；网络失败只使用内存副本重试。
- server session 只保存在进程内，使用独立随机 bearer、30 分钟 idle TTL 和 8 小时 absolute
  TTL。成功的 authenticated GET（含 304）刷新 idle；页面 hidden 时停止 polling，因此不会刷新。
  absolute TTL 永不延长。
- browser 只把 bearer 放在当前 origin、当前 tab 的 `sessionStorage`；不使用 cookie、
  `localStorage`、IndexedDB、URL 或页面可见 DOM。localhost cookie 不按端口隔离，明确禁止。
- API client 只通过 `Authorization: Bearer <session>` 发送；跨站脚本设置该 header 会触发
  preflight，而 server 不允许 CORS。
- session exchange 必须具有精确 `Host`、精确 same-origin `Origin`，以及在浏览器提供时合法的 `Sec-Fetch-Site`。
- session exchange 只接受 `application/json`；数据 GET 必须有合法 Authorization；如请求
  包含 Origin，它也必须精确匹配。

### 9.3 请求与响应限制

- listener 固定 `127.0.0.1`，Host 固定为 `127.0.0.1:<actual-port>`；拒绝缺失、重复或其他
  Host、`localhost` 别名和所有转发 header。
- 只接受 origin-form request target；拒绝 absolute-form、authority-form、asterisk-form、
  obs-fold、header 控制字符、`Transfer-Encoding`、重复或冲突 `Content-Length`。session POST
  必须且只能有一个合法 Content-Length；GET 不接受 body。响应使用 `Connection: close`。
- 只允许协议定义的方法；`OPTIONS` 不启用 CORS，所有 workspace mutation method 返回 405。
- 请求行上限 4 KiB、总 header 上限 16 KiB、session body 上限 512 bytes。
- 同时请求默认最多 8 个；读超时 5 秒，单请求总 deadline 10 秒。
- server 和 handler 不输出访问日志、bootstrap token、bearer、Authorization、query 或异常堆栈。
- 所有数据响应使用 `application/json; charset=utf-8`、`nosniff` 和 `no-store`。
- CSP 至少为：`default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`；同时发送 `Cross-Origin-Opener-Policy: same-origin`。
- 页面不使用 inline script、inline style、`innerHTML`、动态执行、第三方资源或 service worker。

### 9.4 静态资源

构建时生成固定 asset manifest，运行时只按 manifest key 读取 `importlib.resources` 中的普通文件。资源解析不得接受任意 filesystem path；拒绝 symlink、目录、大小漂移和 digest 不匹配。禁止从 cwd 或 source tree fallback；manifest 缺失时 server 启动失败，不能退化为空白页。HTML 不包含项目数据或 bootstrap secret。

## 10. 一致性、性能与部分失败

### 10.1 两层读取

为了让 50 个工作区仍能快速打开，数据读取分两层：

1. **summary capture**：registry、Profile 结构、Task 或 Objective 计数、状态与 attention 摘要；不运行完整 `git status -uall`。
2. **detail inspection**：只为当前和可见 workspace 执行有超时的 Git、集成与 doctor 探测。

初始页面不等待所有 Git 仓库。未探测项显示 `not_inspected`，不能冒充 healthy。

### 10.2 工作区隔离

- registry 结构损坏是一个全局 `registry_unavailable` 状态；Console 保持可打开并提供恢复说明，但绝不覆盖文件。
- Profile 或 Objective 损坏只影响对应 workspace card。
- summary 默认最多 4 个 workspace 并行读取，单 workspace 预算 3 秒，内部总预算 6 秒，外层
  inspection process group 硬预算 8 秒。该预算包含隔离 Python 子进程的启动成本。
- detail 中每个 Git 子进程必须传显式 timeout；超时转换为 `GIT_PROBE_TIMEOUT`。
- HTTP 主进程把 registry 和 workspace inspection 交给 `exec` 启动的内部 worker process；父进程
  使用单调时钟硬 deadline、有界队列和最多 4 个 worker。协作式 cancellation 只用于普通步骤，
  不能作为中断 `stat/open/read` 的安全保证。
- 文件读取前检查大小，parser 只消费一次读取的有界 bytes；若文件系统调用仍不返回，父进程
  终止整个 worker process group 并释放 request/inspection slot。
- Ctrl-C 先停止接收请求，再取消排队 inspection、终止所有 worker process group，并在固定
  shutdown deadline 后关闭 listener；不存在无法回收的 workspace inspection thread。
- 内存 cache 按 workspace snapshot digest、endpoint 和 session 共享，使用条目数与总字节双上限；进程退出即丢弃。

### 10.3 一致性标记

Console 不跨 workspace 取全局锁。每个 workspace 使用一次共享 snapshot capture，并在读取前后核对相关 revision、ledger 或 action chain 摘要。发生并发变化时最多重试一次；仍变化则返回完整的最近一次一致数据并标记 `stale`，或返回 `partial`，不能拼接成看似一致的结果。

overview 的 aggregate digest 由有序 workspace alias、各自 digest 和错误 code 计算，因此某个 workspace 的变化只使对应卡片和总 ETag 变化。

## 11. 前端实现与可用性

### 11.1 渲染与路由

- 原生 ES modules，按 view 分文件；无运行时框架、无 CDN、无构建时联网要求。
- hash route 仅保存安全 ID、筛选和选中节点，不包含 token、路径或敏感状态。
- 所有状态字符串通过 `textContent` 或等价安全节点 API 写入。
- API client 统一处理 304、401、partial、timeout、退避和恢复命令；view 不自行拼接请求。
- 复制命令失败时回退为自动选中文本并说明手工复制方式。

### 11.2 视觉层级

首屏先显示需要处理的异常，再显示活跃工作，最后显示统计。大数字只用于可行动计数，不用“完成百分比”掩盖证据状态。哈希、revision 和底层约束使用次级层级。

每个状态同时具备：图标、文字标签和简短含义。`repair_required` 与 `needs_you` 的视觉区分不只依赖红色或黄色。

### 11.3 可访问性

- 使用语义化 heading、nav、main、table、button 和 status region；
- 全部功能可用键盘完成，焦点顺序与视觉顺序一致；
- route 切换后焦点进入页面标题，刷新失败通过非打断式 live region 通知；
- 对比度达到 WCAG 2.2 AA，支持系统深浅色和 `prefers-reduced-motion`；
- 图节点可从等价表格定位，不能把 SVG 当成唯一信息入口；
- 320 px 宽度仍可完成 workspace、Task 和恢复命令的主要旅程；
- 日期同时提供本地显示与完整 UTC 值，不能只显示“刚刚”。

### 11.4 文案与本地化

API 传稳定 code 和事实，客户端按 locale 渲染文案。首版至少提供英文和简体中文，语言默认跟随浏览器，可在当前 session 切换。未知 code 使用安全通用文案并显示 code，不能静默丢失事项。

## 12. 失败模式与恢复体验

| 失败 | 页面行为 | 推荐恢复 |
| --- | --- | --- |
| 没有登记 workspace | onboarding 空状态 | `dyro setup` / `dyro join` / `dyro workspace add` |
| registry 损坏 | 全局恢复页，不覆盖文件 | `dyro workspace list` |
| stale workspace path | 保留 unavailable 卡片 | `dyro workspace remove <alias>` 或重新 add |
| Profile 无效 | 该卡片 degraded | `dyro --workspace <alias> doctor` |
| Git 探测超时 | 基本状态可用，health partial | 手工运行 `doctor` |
| TaskGraph 无效 | 禁止 readiness 图，列出 issue | `dyro ... task graph check` |
| Objective journal 损坏 | repair 置顶，其他 workspace 可用 | `dyro ... objective repair --check` |
| action uncertain | 显示证据链并停止自动建议 | `dyro ... objective repair` |
| session 过期 | 保留页面框架，清空数据 | 重新运行 `dyro console` |
| 浏览器打开失败 | server 继续前台运行 | 打开一次性本地 URL |
| asset 校验失败 | server fail-closed，不提供降级 HTML | 重新安装当前 Dyro 版本 |

每个错误最多给一条主要恢复命令；次要诊断折叠显示。页面不能提供“忽略并标记完成”。

## 13. 验证策略

### 13.1 读模型

- fixture workspace 的 API 与 CLI Core 查询使用同一 TaskGraph、status、reason code 和 evidence binding；
- 相同权威输入产生相同脱敏 data 与 digest；
- presentation locale 和 capture time 不改变事实 digest；
- raw path、remote、argv、prompt、answer、log、未知 event 字段不进入 JSON；
- 并发变化、损坏文件和超时产生明确 freshness 或 error，不静默降级为空。

### 13.2 HTTP 与安全

- 无 bearer、重复 bootstrap、过期 bootstrap、伪造 Host、跨 Origin、CORS preflight、无效 method 全部拒绝；
- 证明响应不设置 cookie，另一个 localhost port 不能获得 bearer 或调用 authenticated API；
- session exchange 的 Origin 缺失、`null` 或不精确均拒绝；authenticated GET 若带 Origin 必须
  精确匹配，并同时验证浏览器提供的 Fetch Metadata。
- fragment secret 不出现在首个请求、Referer、server log、API error 或 HTML；
- encoded traversal、双重解码、symlink asset、超大 header 或 body、慢请求和并发耗尽测试；
- 恶意 title、reason fact、ledger 字段与 Unicode 控制字符不能生成可执行 DOM；
- fuzz request parser、路径和 JSON serialization；
- 用永不返回的 inspection fixture 证明 overview/request deadline 和 Ctrl-C 均有界，worker
  process group、并发 slot 和队列全部释放。

### 13.3 用户体验

- 新用户从任意目录启动，在 30 秒内找到最高优先级事项；
- 多 workspace 中一个损坏仍能浏览其他项目；
- 只用键盘完成 overview → Task → 复制恢复命令；
- 320、768、1440 px 三档视口；深色、高对比、减少动画；
- 1,000 Task 图默认不会一次渲染全部节点，表格和过滤仍可用；
- 页面隐藏后停止轮询，重新可见后仅触发一次条件刷新。

### 13.4 产物与性能

- 从 clean wheel 和 sdist 安装到 checkout 外，静态 manifest、所有 assets 和 `dyro console` 可用；
- 断网环境功能完整，浏览器请求列表没有外部 origin；
- 50 workspace summary 在目标预算内返回，慢 workspace 被单独标记；
- 1,000 Task detail 与 graph API 受响应上限保护，无 O(N²) 状态扫描；
- 全量 unittest、ruff、构建 metadata、license 和术语策略门禁通过。

## 14. 发布与演进边界

Console 内部发布顺序固定为：读模型 → 安全 listener → 概览 → 详情与图 → 可用性 → 安全和产物硬化。任何阶段未满足只读、脱敏或 localhost 安全门禁时，Console 不进入 `v0.6.0`；CLI 和续航 Core 不受影响。

`v0.5.7` 仅接收维护修复。`v0.6.0` 同时包含持久续航 PR-01 至 PR-12 和 Console C01 至 C06：
PR-09 之后，Console C01–C06 与续航 PR-10、PR-11 可以并行；两线完成后由续航 PR-12 执行
统一安全、迁移、产物和发布候选门禁。统一发布不改变安全边界：Console 仍只读，自动运行仍默认
关闭并要求显式 ActivationLease，自动 push 仍不存在。

未来若要提供浏览器写操作，必须新建 ADR，并至少采用“预览不可变命令 → 显示权限与影响 → 一次性 nonce 和过期时间 → Core 重新验证 → 现有 mutation API”的模式。首个写阶段也不得包含 push、release 或任意 shell。

未来若要提供团队或远程访问，必须作为独立服务设计真实身份、TLS、RBAC、workspace 同步、审计、部署、备份和升级；不得通过给本地 server 增加 `--host 0.0.0.0` 来演化。
