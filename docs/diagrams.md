# 图示导览（新人上手）

本文提供 **PNG 静态图**（GitHub 直接显示）与对应 Mermaid 源文件（`docs/images/diagrams/src/*.mmd`，可改后重新导出）。

重新导出：

```bash
# 需要 Node + npx
python3 scripts/render_diagrams.py
```

| 图 | 文件 |
| --- | --- |
| 1. 系统分层架构 | [01-architecture.png](images/diagrams/01-architecture.png) |
| 2. 多仓工作区目录 | [02-workspace-layout.png](images/diagrams/02-workspace-layout.png) |
| 3. 任务状态机 | [03-task-state-machine.png](images/diagrams/03-task-state-machine.png) |
| 4. 本地交付时序 | [04-local-delivery-sequence.png](images/diagrams/04-local-delivery-sequence.png) |
| 5. 外部证据时序 | [05-external-evidence-sequence.png](images/diagrams/05-external-evidence-sequence.png) |
| 6a. 任务图 | [06a-task-graph.png](images/diagrams/06a-task-graph.png) |
| 6b. 调度 | [06b-scheduling.png](images/diagrams/06b-scheduling.png) |
| 7. 用例总览 | [07-use-cases.png](images/diagrams/07-use-cases.png) |
| 8a. 多智能体分层 | [08a-multi-agent-layers.png](images/diagrams/08a-multi-agent-layers.png) |
| 8b. 多智能体时序 | [08b-multi-agent-sequence.png](images/diagrams/08b-multi-agent-sequence.png) |
| 9a. 外部语义运行时 | [09a-semantic-runtime.png](images/diagrams/09a-semantic-runtime.png) |
| 9b. 语义运行时时序 | [09b-semantic-runtime-sequence.png](images/diagrams/09b-semantic-runtime-sequence.png) |

关联：[`architecture.md`](architecture.md) · [`agent-orchestration-discipline.md`](agent-orchestration-discipline.md) · [English](diagrams.en.md)

---

## 1. 系统分层架构

![1. 系统分层架构](images/diagrams/01-architecture.png)

**读图要点**

- Core **不**内嵌客户仓库名、模型价目或业务规则；这些在 Profile。
- Agent 只通过 adapter argv 启动；gates 由编排器执行，不采信口头成功。
## 2. 多仓工作区目录结构

![2. 多仓工作区目录结构](images/diagrams/02-workspace-layout.png)

**读图要点**

- **Anchor**（`repositories/*`）是登记源；**line worktree** 与 **task worktree** 是隔离检出。
- 任务状态与证据在 **`.dyro/tasks/`**，不写回 Profile 主配置。

```text
workspace/
  dyro.toml
  repositories/…          # anchors
  versions/<line>/…       # 开发线 worktree
  worktrees/task-<id>/…   # 任务 worktree
  .dyro/lines · tasks · changes · ledger.jsonl
```
## 3. 任务状态机

![3. 任务状态机](images/diagrams/03-task-state-machine.png)

非法跳转被拒绝；人工越权需显式 `--force`（见架构文档）。
## 4. 本地交付主流程（时序）

![4. 本地交付主流程（时序）](images/diagrams/04-local-delivery-sequence.png)

## 5. 外部证据交接（时序）

![5. 外部证据交接（时序）](images/diagrams/05-external-evidence-sequence.png)

`policy.execution_mode = "external"` 时，控制机不跑 Agent/gates。
## 6. 任务图与调度

![6. 任务图与调度](images/diagrams/06a-task-graph.png)

- `depends_on`：硬先后。  
- `conflict_group`：资源互斥，**不是**边。  
- `done` 且 **已 merge 进线** 才释放下游。

### 调度波次

![调度波次](images/diagrams/06b-scheduling.png)

## 7. 用例总览

![用例总览](images/diagrams/07-use-cases.png)

---

## 8. 多智能体协作（开发侧）

分层与纪律见 [`agent-orchestration-discipline.md`](agent-orchestration-discipline.md)。实验：`experiments/local_agent_dispatch/`（**不进**安装包）。

![多智能体分层](images/diagrams/08a-multi-agent-layers.png)

![多智能体时序](images/diagrams/08b-multi-agent-sequence.png)

Dispatch 结果仅为**建议**；交付仍走 Dyro gates / merge。

---

## 9. 可选外部语义运行时（实验）

**非 Core。** `experiments/external_workflow_runner/`。生产见 Stage5 `NOT_READY`。

![外部语义运行时](images/diagrams/09a-semantic-runtime.png)

![语义运行时时序](images/diagrams/09b-semantic-runtime-sequence.png)

---

## 10. 新人 15 分钟路径

1. 浏览本页 **§1–§2** 图。  
2. 按 [README](../README.md) Quick start 跑通 `setup` / `doctor`。  
3. 对照 **§4** 走 `task create → run → review → merge`。  
4. external 模式再看 **§5**。  
5. 多 Agent 只读 **§8**；勿把派发结果当 gate。

---

## 维护

- 改图：编辑 `docs/images/diagrams/src/*.mmd`，运行 `python3 scripts/render_diagrams.py`。  
- 图与代码冲突时，以代码与 `architecture.md` 为准。  
