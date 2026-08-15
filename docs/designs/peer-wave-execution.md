# Peer Wave：多 Harness 并行执行面

状态：已接受（随 0.6.x 落地）  
范围：Dyro Core 写路径；不替代 gates / review / merge  
关联：[多智能体编排纪律](../agent-orchestration-discipline.md) · [ADR-0002](../adr/0002-optional-local-agent-dispatch.md) · [可选本地 Agent 派发](optional-local-agent-dispatch.md)

## 1. 产品判断

多 Harness 的默认画面是 **一波 peer 任务同时写**，不是一路改、其余盯梢。

写隔离已经在 Core：

- 一条 Task 同一时刻只有一个 `executor`
- 每个 Task 有自己的 `task/<id>` worktree
- 重叠切片用 `conflict_group` 串行；不重叠的进入同一波

0.6.8 的九路 headless 适配器接到这条写路径上：在 **既有 task worktree** 里跑，不再为交付执行另开 detached patch 树。

## 2. 硬规则

1. 一棵 task worktree 同一时刻只有一个写 harness。不是全工作区只有一个写。
2. 同一 `conflict_group` 同一波只进一个 Task。空 `conflict_group` 在 `max_parallel > 1` 时警告，但不互斥。
3. 波次里每个成员都是执行位。只读意见走 `dyro dispatch panel`；复核是该 Task 的下一阶段，审冻结 HEAD，不看 live 树。
4. `cursor-agent` 不能进入写波次；其 edit 仍 fail-closed。
5. Dispatch 仍然不能 merge / push / signoff。交付证据只走 Core gates。

## 3. 产品路由

| 意图 | 路径 |
| --- | --- |
| 同时改多块 | 拆成 N 条 Task，填 `conflict_group` 与 `executor`，`task daemon --parallel` 或 Objective `max_parallel` |
| 只要第二意见 | `dyro dispatch panel` |
| 还不成 Task 的试改 | `dyro dispatch run`（只读或 detached patch） |
| 禁止默认 | 一个 edit writer + 其余角色陪跑 |

Batch V1 保持建议面：2–4 个独立角色、最多一个 edit。不要在 Batch 里重做 TaskGraph。

## 4. 执行桥

`task.executor` 若是已就绪且支持 edit 的 dispatch Provider，`run_task` 调用该适配器：

- cwd = `_ensure_task_worktrees()` 的根
- 分支 = `task/<id>`
- 使用适配器自己的隔离 Home 与进程监督
- 不创建 `EditWorkspace`，不回写源工作区之外的 detached tree
- 终态仍走 receipt → gates → review → merge

未就绪或非 dispatch 的 executor 回退到 Profile `write` argv，避免没有本机登录时阻断既有工作区。`auto` 只从就绪的可写 Provider 里确定性分配。

## 5. 异构波次

`objective tick` 与 `task daemon` 预览为每个 ready Task 绑定空闲就绪 harness：

- 钉死的 `executor` 优先；达到每后端上限则推迟，不静默换人
- `auto` 按 Provider id 排序领取空闲可写 harness
- 遵守 Objective / daemon 并行容量、`conflict_group`、每后端上限
- Cursor 作为写 executor 被拒绝

Objective 默认 `max_parallel = 3`。本机有就绪可写 Provider 时，有效容量为 `min(requested, ready_write_count)`；没有就绪 Provider 时保持 requested，以便 Profile-only 工作区继续跑。
