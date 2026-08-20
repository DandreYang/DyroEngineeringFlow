# 多智能体编排纪律（first-party）

本文是 Dyro 及相关工程在**本地多 Agent 协作**时的强制纪律，独立于外部协作工具。
它适用于：Workflow / 并行 subagent 扇出、跨仓审计、对抗评审、跨会话续作。

关联：

- [图示导览 §8 多智能体协作](diagrams.md#8a-多智能体分层实验)
- [ADR-0002：可选本地 Agent 派发与结果密封](adr/0002-optional-local-agent-dispatch.md)
- [可选本地 Agent 派发设计](designs/optional-local-agent-dispatch.md)
- [实现：`experiments/local_agent_dispatch/`](../experiments/local_agent_dispatch/README.md)（L0–L4 CLI）

## 1. 分层：Harness / 记录 / 控制面

| 层 | 职责 | 禁止 |
| --- | --- | --- |
| **Harness（编排）** | 预算、模型路由、异步派发、缓存失效、验真方法 | 把 harness 结论当成 gate |
| **记录（评审板）** | 签名区、P0/P1/P2、Go/No-Go、交接指令 | 互相改写他人签名区 |
| **控制面（Dyro）** | claim、worktree、证据、复核、签收、合并 | 被外部 Agent 直接 merge/push |

Agent 输出永远是**建议**；进入交付链必须经过 Dyro 契约与独立主体。

## 2. 扇出执行五原则

### 2.1 验真必须查看真实产物

禁止用与发现者相同的二次搜索「确认」结论。

- 发现者 grep 类名 → 验证者必须打开渲染产物 / 运行路径 / 对端契约。
- 默认立场：**不确定则驳回**，不让「看起来合理」的断言存活。

### 2.2 执行前重验计划前提

计划写完到执行之间，环境会腐坏。负载前提至少重查：

- `git status` / `git log`（是否已提交、是否被覆盖）
- 目标文件是否仍存在、默认配置是否仍成立
- 依赖的 PR/分支 tip 是否变化

### 2.3 按 量×单价 与质量闸门路由模型

- **海量读取 / 粗扫**：够用且总价最低的模型  
- **对抗验证 / 仲裁 / 终评**：最强模型、少调用  

禁止按「名气」路由。

### 2.4 缓存/续聊对 substrate 失明 → 主动失效

`(prompt, opts)` 或 session resume **不知道**读过的文件已变。

- substrate 变更后：必须改动共享 preamble 或显式 bust  
- 故意保留某一缓存时：保证该 agent 的 key 字节级不变  

### 2.5 事先预算 agent×tokens

估不进会话/配额则：减 agent、批处理验证、换 bulk 模型、拆阶段。  
任何缩放妥协必须在报告中写明，禁止静默「看起来全覆盖」。

## 3. 对抗评审记录协议（摘要）

第一方座位 `dyro-board` 在用户提出会审 / 对抗 / Go/No-Go 时自动戴上协议；人类命令是 `/dyro-review-board`。记录仍不是 Proof。完整模板见 [可选本地 Agent 派发设计 §6](designs/optional-local-agent-dispatch.md#6-对抗评审记录协议)。

硬规则：

1. 单一共享评审文件  
2. 每人仅写自己的签名区  
3. **源码 / 线上契约 > 计划 / 旧评审**  
4. 无法证明 → 标 `须人工核`  
5. 终裁合并重复、仲裁冲突、输出 P0/P1/P2 + Go/No-Go + 执行交接  

建议路径：

```text
docs/reviews/YYYY-MM-DD-<topic>-adversarial-board.md
```

## 4. 写冲突与脏工作区

| 规则 | 说明 |
| --- | --- |
| 一棵 task worktree / 同一 `conflict_group` 同时仅一个写 agent | 并行写同一 checkout 会覆盖已验证修复；不同 task 树或不同冲突组可以同时写 |
| 改前 `git log -5 -- <path>` | 避免回退到已废弃的 workaround |
| 精确 stage | 禁止盲目 `git add .`；同文件无关 hunk 必须拆分 |
| 「验证后又坏了」 | 先查是否有后续 commit 覆盖，再查运行时 |

## 5. 跨会话取证分级

| 级别 | 含义 | 可否当作当前真相 |
| --- | --- | --- |
| `confirmed-current` | 当前 checkout 或命令可复现 | 是 |
| `prior-claim` | 旧会话断言，未复验 | 否 |
| `useful-pattern` | 可升 skill / 纪律的重复教训 | 仅作流程 |
| `stale-or-unknown` | 漂移或样本不足 | 否 |

旧会话记录是 **线索**，不是 SSOT。

## 6. 多仓交付清单

1. 各相关仓 `git status --short --branch`  
2. 区分「全部脏变更」与「本轮变更」  
3. 保护无关 WIP  
4. 精确 stage + 仓内校验  
5. 中文 Conventional Commit（除非仓规另定）  
6. 仅在明确要求时 push，并用 `ls-remote`/fetch 核远端  

## 7. 开跑前检查表

1. 是否已有 skill/纪律覆盖？不要重建 harness  
2. 任务是否拆成维度与验收标准？  
3. agent×tokens 是否进预算？  
4. bulk vs quality 路由是否已设定？  
5. 验证者是否被要求「打开真产物并尝试证伪」？  
6. 若 resume：substrate 是否变化？  
7. 是否声明缩放妥协？  
8. 写路径是否保证每棵 task worktree / 每个 conflict_group 只有一个写 agent，且波次成员都是执行位？
9. 结果是否只回收摘要/契约字段，而非完整事件流？
