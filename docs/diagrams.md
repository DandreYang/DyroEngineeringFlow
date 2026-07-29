# 图示导览（新人上手）

本文用 **Mermaid** 汇总 DyroEngineeringFlow 的关键结构与协作路径。GitHub 可直接渲染；本地可用支持 Mermaid 的 Markdown 预览。

| 图 | 用途 |
| --- | --- |
| [1. 系统分层架构](#1-系统分层架构) | Core vs Profile、职责边界 |
| [2. 多仓工作区目录结构](#2-多仓工作区目录结构) | 落地后磁盘长什么样 |
| [3. 任务状态机](#3-任务状态机) | 合法状态与跳转 |
| [4. 本地交付主流程（时序）](#4-本地交付主流程时序) | 从建线到 merge |
| [5. 外部证据交接（时序）](#5-外部证据交接时序) | claim → build → import → review → signoff |
| [6. 任务图与调度](#6-任务图与调度) | depends_on / conflict_group |
| [7. 用例总览](#7-用例总览) | 角色与主要用例 |
| [8. 多智能体协作（开发侧）](#8-多智能体协作开发侧) | 派发 / 评审板 / 控制面分层 |
| [9. 可选外部语义运行时（实验）](#9-可选外部语义运行时实验) | Sandbox / Broker / Supervisor |

关联：[`architecture.md`](architecture.md) · [`agent-orchestration-discipline.md`](agent-orchestration-discipline.md) · [ADR-0001](adr/0001-optional-external-semantic-runtime.md) · [ADR-0002](adr/0002-optional-local-agent-dispatch.md)

---

## 1. 系统分层架构

```mermaid
flowchart TB
  subgraph Profile["Project Profile（团队提供）"]
    P1["repositories / layout / 基线"]
    P2["Agent adapters argv"]
    P3["gates / 回执模板 / 策略"]
  end

  subgraph Core["Dyro Core · dyro CLI（机制）"]
    W["workspace<br/>anchors · lines · doctor"]
    L["launch<br/>安全 argv 模板"]
    D["dispatch<br/>DAG · claim · 状态机"]
    V["verify<br/>gates · ledger"]
    M["merge<br/>预检 · 恢复 · push 策略"]
  end

  subgraph Runtime["工作区运行态 .dyro/"]
    R1["tasks / lines / changes"]
    R2["evidence · review · ledger"]
  end

  Profile --> Core
  Core --> Runtime
  Human["工程师 / 发布负责人"] --> Core
  Agent["本机 Agent CLI"] --> L
  Runner["隔离 Runner（可选）"] -.->|"evidence ZIP"| D
```

**读图要点**

- Core **不**内嵌客户仓库名、模型价目或业务规则；这些在 Profile。
- Agent 只通过 adapter argv 启动；gates 由编排器执行，不采信口头成功。

---

## 2. 多仓工作区目录结构

示意在 `execution_mode = local` 且 anchors 在 `repositories/` 下的常见布局：

```mermaid
flowchart TB
  WS["workspace 根<br/>dyro.toml"]
  WS --> REPO["repositories/"]
  WS --> DYRO[".dyro/"]
  WS --> VER["versions/ 或 layout.lines"]
  WS --> WT["worktrees/ 或 layout.tasks"]

  REPO --> API["services/api · Git anchor"]
  REPO --> WEB["services/web · Git anchor"]

  DYRO --> LINES["lines/&lt;id&gt;.toml"]
  DYRO --> TASKS["tasks/&lt;id&gt;/"]
  DYRO --> CHG["changes/ · decisions · ledger"]

  TASKS --> TT["task.toml · handoff.md"]
  TASKS --> EV["evidence-imports/ · review.md"]

  WT --> TAPI["task/API-101/services/api"]
  WT --> TWEB["task/API-101/services/web"]

  VER --> LAPI["release-…/services/api worktree"]
  VER --> LWEB["release-…/services/web worktree"]
```

**读图要点**

- **Anchor**（`repositories/*`）是登记源；**line worktree** 与 **task worktree** 是隔离检出。
- 任务状态与证据在 **`.dyro/tasks/`**，不写回 Profile 主配置。

文本树（与架构文档一致）：

```text
workspace/
  dyro.toml
  repositories/…          # anchors（可选 remote）
  versions/<line>/…       # 开发线 worktree（layout 可配置）
  worktrees/task-<id>/…   # 任务 worktree
  .dyro/
    lines/ · hotfixes/ · tasks/ · changes/
    decisions.toml · ledger.jsonl
```

---

## 3. 任务状态机

```mermaid
stateDiagram-v2
  [*] --> backlog
  backlog --> assigned: claim / next 领取
  assigned --> in_progress: run 开始
  in_progress --> waiting_answer: 需要人工答案
  waiting_answer --> in_progress: task answer
  in_progress --> review: gates 通过 · 进入复核
  in_progress --> failed: 失败
  failed --> assigned: 重试领取
  review --> review_pending_signoff: require_external_signoff
  review --> done: 独立复核 PASS
  review_pending_signoff --> done: task signoff
  done --> [*]: task merge 进开发线
```

非法跳转被拒绝；人工越权需显式 `--force`（见架构文档）。

---

## 4. 本地交付主流程（时序）

```mermaid
sequenceDiagram
  actor Eng as 工程师
  participant CLI as dyro CLI
  participant FS as 工作区 Git / .dyro
  participant Agent as Agent adapter

  Eng->>CLI: setup / doctor / line create
  CLI->>FS: 登记 line · 创建 line worktrees
  Eng->>CLI: task create · task next
  CLI->>FS: 写 task.toml · 分配 worktree
  Eng->>CLI: task run / open --agent
  CLI->>Agent: argv launch 到 task worktree
  Agent-->>CLI: 工作结束（非 gate 证据）
  CLI->>CLI: 执行 Profile gates
  CLI->>FS: receipt · heads · attempt
  Eng->>CLI: task review
  CLI->>FS: 绑定 receipt 的 review
  Eng->>CLI: task merge --yes
  CLI->>FS: 合入开发线 · 更新 ledger
```

---

## 5. 外部证据交接（时序）

`policy.execution_mode = "external"` 时，控制机不跑 Agent/gates：

```mermaid
sequenceDiagram
  actor Ctrl as 控制面操作者
  participant CLI as dyro 控制面
  participant Run as 隔离 Runner
  participant Rev as 独立复核方

  Ctrl->>CLI: task claim --by runner-id
  CLI-->>Run: claim 生效 · 冲突组占用
  Run->>Run: 在隔离区执行 · 跑声明 gates
  Run->>CLI: evidence build → ZIP
  Ctrl->>CLI: evidence execution --bundle
  CLI->>CLI: 校验 ZIP · heads · gates · 签名策略
  Rev->>CLI: evidence review / review-build
  CLI->>CLI: 绑定 receipt + task-heads
  opt require_external_signoff
    Ctrl->>CLI: task signoff --by approver
  end
  Ctrl->>CLI: task merge --yes
```

---

## 6. 任务图与调度

```mermaid
flowchart LR
  subgraph Nodes
    T1["Task A"]
    T2["Task B"]
    T3["Task C"]
    D1["Decision<br/>blocked_on"]
  end

  T1 -->|depends_on| T2
  T2 -->|depends_on| T3
  T3 --> D1

  CG["conflict_group: db-migrate<br/>同一 wave 互斥"]
  T1 -.-> CG
  T2 -.-> CG
```

```mermaid
flowchart TB
  Snap["不可变调度快照<br/>graph + 状态 + claims"]
  Ready["ready set<br/>依赖已集成 · 决策满足"]
  Wave["本轮 wave<br/>--parallel × conflict_group"]
  Snap --> Ready --> Wave
```

- `depends_on`：硬先后。  
- `conflict_group`：资源互斥，**不是**边。  
- `done` 且 **已 merge 进线** 才释放下游（HEAD 祖先检查）。

---

## 7. 用例总览

```mermaid
flowchart LR
  subgraph Actors
    Dev["开发工程师"]
    Lead["版本 / 发布负责人"]
    Runner["隔离 Runner"]
    Reviewer["独立复核人"]
  end

  subgraph UC["主要用例"]
    U1["初始化工作区 setup/init"]
    U2["创建开发线 / Hotfix"]
    U3["创建与调度任务"]
    U4["本地执行与 gates"]
    U5["外部证据导入"]
    U6["独立复核与签收"]
    U7["合并与 Change Set 校验"]
    U8["审计同步 Witness"]
  end

  Dev --> U1
  Dev --> U3
  Dev --> U4
  Lead --> U2
  Lead --> U7
  Lead --> U8
  Runner --> U5
  Reviewer --> U6
  Dev --> U6
```

---

## 8. 多智能体协作（开发侧）

分层与纪律见 [`agent-orchestration-discipline.md`](agent-orchestration-discipline.md)。实验实现：`experiments/local_agent_dispatch/`（**不进** `dyro` 安装包）。

```mermaid
flowchart TB
  Host["宿主 Agent<br/>当前对话"]
  Disp["local_agent_dispatch<br/>契约 · 守卫 · 租约"]
  B1["后端 CLI A"]
  B2["后端 CLI B"]
  Board["对抗评审板<br/>签名区 + 终裁"]
  Dyro["Dyro 控制面<br/>claim · gates · merge"]

  Host -->|"TaskContract JSON"| Disp
  Disp --> B1
  Disp --> B2
  B1 -->|"summary + evidence"| Host
  B2 -->|"summary + evidence"| Host
  Host --> Board
  Board -.->|"仅建议"| Host
  Host -->|"显式 dyro 命令"| Dyro
```

```mermaid
sequenceDiagram
  participant H as 宿主 Agent
  participant S as DispatchSupervisor
  participant W as Worker / 后端
  participant P as 评审板文件

  H->>S: run --wait TaskContract
  S->>S: 校验 files · secret guard · 占槽
  S->>W: 自包含 prompt + 白名单上下文
  W-->>S: ResultEnvelope
  S->>S: locator verified 标记
  S-->>H: run_id · summary · evidence
  H->>P: 写入本人签名区
  Note over H,P: 不得改他人区；源码为准
  H->>H: 终裁后可选改代码 / 提 PR
  Note over H: 合入交付仍走 dyro task/merge
```

---

## 9. 可选外部语义运行时（实验）

**非 Core。** 路径：`experiments/external_workflow_runner/`。生产仍见 Stage5 `NOT_READY`。

```mermaid
flowchart TB
  Sup["可信 Supervisor"]
  Sand["Workflow Sandbox<br/>固定 TS bundle · 无供应商 token"]
  Bro["Agent Broker<br/>argv provider · raw 仅 tmpfs"]
  HostP["可选 host provider<br/>仅挂 Broker"]

  Sup -->|启动 · 校验 bundle/claim| Sand
  Sup -->|启动 · pin| Bro
  Sand -->|loopback IPC| Bro
  HostP -.->|RO bind| Bro
  Sup -->|双重清理后| Pack["本地 evidence pack / dry-run"]
  Pack -.->|禁止| Merge["merge / push / Core import"]
```

```mermaid
sequenceDiagram
  participant S as Supervisor
  participant B as Broker container
  participant W as Sandbox container

  S->>B: start internal net + pin
  S->>W: start shared netns · no token
  W->>B: agent.call JSON-line
  B->>B: spawn provider · destroy raw
  B-->>W: sanitized result
  W-->>S: result-envelope + artifacts
  S->>W: cleanup verify
  S->>B: stop · containers absent
  S->>S: pack only if dual cleanup OK
```

---

## 10. 新人 15 分钟路径（对照图）

1. 读本文 **§1–§2** → 建立空间感。  
2. 按 [README](../README.md) **Quick start** 跑通 `setup` / `doctor`。  
3. 对照 **§4** 走一遍 `task create → run → review → merge`（可用 noop adapter）。  
4. 若团队是 external 模式，再读 **§5** 与 [architecture 外部契约](architecture.md)。  
5. 多 Agent 协作只读 **§8** 与纪律文档；勿把派发结果当成 gate。

---

## 维护说明

- 图与代码冲突时，**以代码与 `architecture.md` 为准**，并回写本页。  
- 新增主路径行为时，优先补 **时序图或状态机**，避免只加散文。  
- 实验图（§8–§9）变更不应迫使 Core 版本号变化。
