# ADR-0002：可选本地 Agent 派发与结果密封

- 状态：已接受（2026-07-30）
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
若把任意第三方协作工具直接引入 Core，会重演「编排库 = 控制面」的边界错误（见 ADR-0001 修订结论）。

因此采用 **first-party 设计**：协议与安全边界由本仓库定义与实现、可移除、可测试；具体 CLI 适配器可后置实现，且永不成为 Dyro 运行时依赖。

## 决策

1. 允许在仓库内以 **可移除实验模块** 形式演进「本地 Agent 派发」协议（见 `experiments/local_agent_dispatch/`）。
2. 协议必须包含：五段式任务契约、文件白名单、注入前机密守卫、可选严格影子目录、异步 run 生命周期、结果契约（含 locator 核验）、进程身份租约、edit 模式仅 patch 交付。
3. 默认结果为 **建议性**；核验字段标记可信度，不静默删除条目。
4. **禁止** 从派发 Supervisor 调用 signoff / merge / push / 生产 evidence import。
5. 与 ADR-0001 的 Docker 语义运行时 **并列不合并**：派发 harness 不替代 Sandbox/Broker；语义运行时不替代多宿主 CLI 派发。
6. 宿主 skill 应按本机探测到的后端 **动态渲染**，不得引导调用未安装/未登录后端。

## 后果

- 获得可审计、可测试的一等公民设计，不绑定外部品牌或仓库。
- 实现可分阶段：先契约与守卫（可单测），后 CLI 适配器。
- 生产仍由 ADR-0001 / Stage5 `NOT_READY` 门禁约束；本 ADR 不降低生产门槛。

## 否决项

- 将本地派发 harness 加入已安装 `dyro` 包依赖。
- 用多模型投票替代 Dyro gates。
- 在非严格模式下假设「权限档 = 物理隔离」。
- 直接修改用户主工作区作为 edit 默认路径。
