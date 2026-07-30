# ADR-0002：可选本地 Agent 派发与结果密封

- 状态：已接受（2026-07-30）；**分发面修订** 2026-07-30（随 `dyro` wheel 安装，仍不替代 Core）
- 日期：2026-07-30
- 决策者：DyroEngineeringFlow 维护者
- 关联：
  - [多智能体编排纪律](../agent-orchestration-discipline.md)
  - [可选本地 Agent 派发设计](../designs/optional-local-agent-dispatch.md)
  - [ADR-0001：可选外部语义运行时](0001-optional-external-semantic-runtime.md)

## 背景

工程交付中经常需要：

- 第二意见 / 多模型交叉评审；
- 大范围只读调研而不污染主对话上下文；
- 在隔离副本中改代码并以 patch 交付；
- 异步派发，不阻塞主 agent 会话。

这些能力属于**开发者侧 harness**，与 Dyro 的跨仓交付控制面（claim、gates、证据、复核、合并）不同层。
若把任意第三方协作工具直接引入 Core 调度/状态机，会重演「编排库 = 控制面」的边界错误（见 ADR-0001 修订结论）。

因此采用 **first-party 设计**：协议与安全边界由本仓库定义与实现、可测试；具体 CLI 适配器可后置；**永不**成为「交付是否成功」的证据源。

## 决策

1. 在仓库内以 **`experiments/local_agent_dispatch/`** 演进「本地 Agent 派发」协议（L0–L4 已实现）。
2. **分发面（2026-07-30 修订）**：该模块**随 `dyro` wheel 一并安装**，入口为：
   - `dyro dispatch …`
   - `python -m experiments.local_agent_dispatch …`
   - `import experiments.local_agent_dispatch`
   
   相对 Core 仍为**可选产品面**：不写入 TaskGraph 成功条件，不成为 gate 证据。
3. 协议必须包含：五段式任务契约、文件白名单、注入前机密守卫、可选严格影子目录、异步 run 生命周期、结果契约（含 locator 核验）、进程身份租约、edit 模式仅 patch 交付。`strict` 只允许声明并实现严格隔离能力的 adapter；当前外部 Codex / Claude CLI 均不满足该门槛，严格任务只可使用离线 `echo` 验证器，或改走 ADR-0001 Docker 隔离链。
4. 默认结果为 **建议性**；核验字段标记可信度，不静默删除条目。
5. **禁止** 从派发 Supervisor（及 `dyro dispatch` 路径）调用 signoff / merge / push / 生产 evidence import。
6. 与 ADR-0001 的 Docker 语义运行时 **并列不合并**：派发 harness 不替代 Sandbox/Broker；语义运行时不替代多宿主 CLI 派发。
7. 宿主 skill 应按本机探测到的后端 **动态渲染**，不得引导调用未安装/未登录后端。

## 后果

- 用户 `pipx install dyro` 后即可使用派发 harness，无需单独 clone 实验树。
- Core 依赖集仍保持轻量（派发不引入 Docker/模型 SaaS 硬依赖）。
- 实现可分阶段：契约与守卫可单测；CLI 适配器可探测可用性。
- 默认 `run` 启动 detached worker；`--wait` 才同步等待。运行状态与租约均通过带 owner token 的原子 claim/续租/释放保护。
- detached worker 的 PID、启动代际与 owner token 持久化；新 Supervisor、result/wait 与 GC 可在原派发进程退出后安全终态化已死亡的 worker。
- `run` / `panel` / worker 的进程树监管当前限定 POSIX（Linux/macOS）；Windows 仅支持导入与只读 discovery，执行 fail-closed。
- `edit` 在 detached Git worktree 内执行并只返回 hash 密封的 patch；主工作区、commit 与 push 均不由派发链修改。
- 生产语义运行时仍由 ADR-0001 / Stage5 `NOT_READY` 门禁约束；本 ADR 不降低该门槛。

## 否决项

- 用多模型投票或 dispatch 结果 **替代** Dyro gates / review / signoff / merge。
- 将派发 Supervisor 接到 Core 的 merge/push/signoff/生产 evidence import 路径。
- 把 CLI 的 plan/read-only/工具禁用权限档或 shadow `cwd` 宣称为 OS 级物理隔离。
- 直接修改用户主工作区作为 edit 默认路径。
- 把第三方协作品牌/SDK 作为 Core 或派发协议的硬依赖。

## 历史说明

- 初版否决项曾写「不得加入已安装 `dyro` 包」。维护者决定改为 **随 wheel 分发、边界仍隔离 Core**，以降低获取成本；本文件 supersede 该否决表述。
