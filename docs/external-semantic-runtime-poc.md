# 外部语义运行时 PoC（first-party）

## 目标

验证 Dyro 能否通过**可选、可移除**的外部执行链，在任务内部安全地运行固定 TypeScript 语义工作流，同时保持任务、Git、gates、证据、复核与合并边界不变。

本 PoC 依据 [ADR-0001](adr/0001-optional-external-semantic-runtime.md)。它验证隔离与集成是否可行，不承诺产品化。

## 非目标

- 不替换 Dyro TaskGraph、scheduler、daemon 或状态机。
- 不把 Bun、TypeScript 语义运行时加入 Dyro Core 依赖。
- 不修改 `done`、review、signoff、merge 或 push 规则。
- 不把 Agent 输出改名为 gate evidence。
- 不执行任意用户提供、运行时生成或未复核的 Workflow。

## 运行时身份

语义流原语位于：

```text
experiments/external_workflow_runner/ts_runtime/   # @dyro/semantic-flow
```

打包进入 sandbox bundle 时使用目录：

```text
vendor/dyro-semantic-flow/
```

身份锁定：

- `implementation`: `dyro-semantic-flow`
- `version`: `1.0.0`
- `content_sha256`: 对 `ts_runtime/` 树的确定性哈希（见 `runtime-lock.json`）

**不**使用任何第三方 workflow npm 包作为编排实现。

## 三个信任域

### 1. Workflow Sandbox

- 运行固定已审 bundle + first-party semantic-flow；
- 只读 rootfs / bundle；任务 worktree 可写；
- 默认无外网；仅可访问 Broker 窄 IPC（internal net + loopback）；
- 环境 allowlist；无 execution key、无供应商 token。

### 2. Agent Broker

- 独立容器 / 进程；
- 窄 JSON-line 协议；
- argv-only provider 或 fixture；raw 输出仅落 tmpfs 并销毁；
- 不持有 Dyro 签名密钥。

### 3. 可信 Supervisor

- 验证 claim、bundle、canonical input；
- 监督 Sandbox / Broker 清理；
- 清理完成前不得挂载 execution key；
- Stage4 起可在 **双重清理验证通过后** 打包本地实验 evidence pack；
- Stage5 起可对 sealed pack 做 **非生产 dry-run** 校验；
  仍不 signoff / merge / push，也不导入 Dyro Core。

## 验收映射（摘要）

隔离、结果 envelope、产物边界、claim 续租、raw 销毁、provider 钉扎、
双重清理、清理后 evidence pack、host provider、dry-run 与生产门禁等项由
`experiments/external_workflow_runner` Stage0–5 测试与报告覆盖。

终评见：

- [`experiments/external_workflow_runner/stage5/POC_EVALUATION.md`](../experiments/external_workflow_runner/stage5/POC_EVALUATION.md)
- [`experiments/external_workflow_runner/stage5/PRODUCTION_NOT_READY.md`](../experiments/external_workflow_runner/stage5/PRODUCTION_NOT_READY.md)

**本地隔离：已证明。Stage5→Core 签名证据交接：已实现。生产：
NOT_READY（仍缺真实环境证据）。**

2026-07-30 的 Production Candidate 晋级契约见
[ADR-0003](adr/0003-external-runtime-production-promotion.md) 与
[生产就绪设计](designs/external-runtime-production-readiness.md)。

## 停止条件

- 必须修改 Dyro 调度/状态机才能表达内部 phase；
- 无法隔离凭证与 execution key；
- 无法 fail-closed 映射 critical 失败；
- 引入第三方工作流源码拷贝或重新引入非 first-party 编排依赖。
