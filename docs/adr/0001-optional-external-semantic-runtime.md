# ADR-0001：可选外部语义运行时（first-party）

- 状态：已接受（2026-07-29 修订：本仓库语义流运行时，不再依赖外部工作流包）
- 日期：2026-07-29
- 决策者：DyroEngineeringFlow 维护者
- 关联文档：[外部语义运行时 PoC](../external-semantic-runtime-poc.md)

## 背景

DyroEngineeringFlow 是跨仓库工程交付控制面。它负责开发线、任务 worktree、依赖调度、冲突组、确定性 gates、执行证据、独立复核、签收、合并与审计。

任务**内部**有时需要短生命周期的语义编排（并行分析分支、分 phase 调用 Agent）。该编排层：

- 不应进入 Dyro Core 运行时依赖；
- 不得持有 Git / 签名 / 供应商凭证；
- 不得替代 TaskGraph、claim、gates 或交付状态机。

早期曾评估过将第三方 TypeScript Agent Workflow 库作为候选。评估结论是：**隔离与责任边界必须自建**；第三方编排库不是交付控制面的一部分，且会引入品牌、供应链与默认 Agent 凭证继承等问题。因此改为 **first-party `@dyro/semantic-flow`**，并与 `experiments/external_workflow_runner` 隔离壳一体演进。

## 决策

1. 允许通过可选外部实验路径运行**固定、已审**的 TypeScript workflow bundle。
2. 语义流原语（`parallel` / `pipeline` / `phase` / 注入式 Agent 绑定）使用仓库内 **first-party** 实现，**不**依赖第三方工作流 npm 包。
3. 隔离架构保持三域：Workflow Sandbox、Agent Broker、可信 Supervisor / Packager。
4. Sandbox 不持有供应商凭证与 execution key；Agent 仅经 Broker 窄 IPC。
5. 成功结果以 schema 化 result envelope 为准；Agent 自述不能代替 Dyro gates。
6. evidence / review / signoff / merge / push 仍仅由 Dyro 控制面与独立主体完成；实验 Supervisor 不得越权。

## 本仓库运行时相对常见外部 Agent 工作流库的差异

| 维度 | `@dyro/semantic-flow` |
| --- | --- |
| 并行失败 | 默认 fail-closed；提供 `parallelSettled`，禁止静默 `null` |
| 默认 Agent | **无**；必须注入 Broker 后端 |
| 凭证 | 运行时不读取 `process.env` 中的密钥 |
| 身份 | 对 `ts_runtime/` 树做 content-hash 锁定 |
| 产品边界 | 仅 experiment vendor，不进 `dyro` 安装包 |

## 后果

- 实验目录可移除而不影响 Dyro Core 安装与官方测试。
- 运行时演进由本仓库维护，无第三方 workflow 包升级耦合。
- 若未来评估其他第三方库，必须作为**新的供应链候选**重新做威胁建模；不得与 first-party 实现混名。

## 否决项

- 用外部语义运行时替换 Dyro TaskGraph。
- 将外部语义运行时加入 Dyro Core 依赖。
- 把 Agent 输出当作 gate 通过条件。
- 复制任何外部工作流库源码并宣称是本仓库实现。
