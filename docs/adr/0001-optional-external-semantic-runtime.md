# ADR-0001：将 外部语义运行时 限定为可选外部语义运行时

- 状态：已接受
- 日期：2026-07-29
- 决策者：DyroEngineeringFlow 维护者
- 关联文档：[外部语义运行时 PoC](../external-semantic-runtime-poc.md)

## 背景

DyroEngineeringFlow 是跨仓库工程交付控制面。它负责开发线、任务 worktree、依赖调度、冲突组、确定性 gates、执行证据、独立复核、签收、合并与审计。

[外部语义运行时](https://example.invalid/semantic-flow) 是以 TypeScript 表达控制流的 Agent Workflow 运行时。它提供 `agent()`、`parallel()`、`pipeline()`、phase、TUI 和 JSONL 事件，适合在一次任务执行内部组织语义工作，但不提供 Dyro 所需的持久任务状态、跨仓 Git 交付、证据绑定与审批控制。

本次评估发现必须区分两个上游身份：

- npm 已发布候选为 `@dyro/semantic-flow@0.2.0`；上游同版本 source tag `v0.2.0` 的 peeled commit 为 `73c61156197445be4a0fad390e3a1d802f2cda4a`；
- 评估时上游 `main` 的提交为 `4cf5f2fc6804f0c65807023e00133a017197c920`，它包含 tag 之后的未发布变更，不能再称为 `0.2.0` 发布物。

PoC 选择已发布 npm 包，并固定包完整性。若后续改为源码 checkout，必须把它视为另一项候选，重新固定提交和依赖闭包，不能混用版本号、tag 与 `main` 提交。

两者的“工作流”处在不同层级：

```text
Dyro TaskGraph 与交付控制
  └─ 可信 Supervisor / Packager
       ├─ 不可信 Workflow Sandbox
       └─ 隔离 Agent Broker
            └─ Codex、Claude 或其他 Agent Runtime
```

## 决策

允许通过独立 Profile 扩展或外部 Runner 试验 外部语义运行时，但不把它加入 Dyro Core 的运行时依赖，也不让它成为 Dyro TaskGraph 的调度器。

外部语义运行时 只作为不可信 Workflow Sandbox 内的可选语义运行时。它不得直接持有 Agent 供应商凭证、Dyro CLI、Git 凭证、execution/review/signoff 私钥，也不得在运行时创建、修改或下载新的 Workflow。所有 Agent 调用通过独立 Agent Broker 的窄接口完成；证据构建由 Workflow 退出并完成隔离清理后的可信 Supervisor / Packager 执行。

该集成必须遵守以下边界：

1. Dyro 是任务状态、依赖、claim、冲突组和交付结果的唯一事实来源。
2. 外部语义运行时 只能执行预先复核且加入 allowlist 的固定 Workflow bundle。
3. Workflow Sandbox 只能写 Dyro 指定的任务 worktree 和自身临时目录。
4. 外部语义运行时 的成功结果、事件或 Agent 自述不能代替 Dyro 声明的 gates。
5. 任务只有在执行证据导入、独立复核和可选签收完成后才能进入 `done`。
6. Workflow Sandbox 与 Agent Broker 均不能调用 Dyro 的 evidence build、merge、push、review、signoff 或强制状态转换。
7. 外部语义运行时 的并发、超时和失败语义必须由 Broker 与 Supervisor 收紧；不能直接沿用“无限并发”或“失败转为 `null` 后继续”的默认行为处理关键步骤。
8. Workflow 必须产生可验证的最终结果协议；自由文本、进程退出码或 `null` 过滤后的汇总不能单独代表成功。
9. 外部语义运行时 的引入不能改变现有外部证据包的验证、安全 ZIP、签名或 review binding 规则。

## 信任域与所有权

### Workflow Sandbox

Workflow Sandbox 运行 外部语义运行时 和固定 Workflow bundle，按不可信代码处理：

- 根文件系统和 Workflow bundle 只读；
- 只有任务 worktree 与隔离临时目录可写；
- 默认无网络，只允许访问 Agent Broker 的本地窄 IPC；
- 不提供供应商凭证、Git 凭证、Dyro CLI 或任何签名私钥；
- 进程环境使用显式 allowlist，不继承 Supervisor 的完整环境；
- 禁止符号链接逃逸、路径穿越、运行时安装和动态 Workflow 生成。

### Agent Broker

Agent Broker 是独立进程或服务，实现 语义运行时 `Agent` 接口的窄适配层：

- 单独持有完成 Agent 调用所需的最小供应商凭证；
- 执行最大并发、单 Agent deadline、输入/输出大小、schema 与模型 allowlist；
- 供应商 CLI 运行在短生命周期的隔离临时文件系统中；
- 未脱敏的供应商输出不能离开该临时文件系统，销毁前只输出经过脱敏的结果和遥测；
- 不持有 Dyro CLI、Git 凭证或 execution/review/signoff 私钥；
- 取消或超时时必须终止完整后代进程树，并验证没有遗留子进程。

### 可信 Supervisor / Packager

可信 Supervisor / Packager 不执行 Workflow 代码：

- 验证 claim、租约、固定 bundle、输入和运行身份；
- 创建并监督 Workflow Sandbox 与 Agent Broker；
- 验证结构化结果、关键分支和产物 realpath、类型、大小与哈希；
- 关闭隔离域并确认没有遗留进程后，才临时挂载 execution key；
- 使用 `dyro task evidence build --claim ...` 为 `DONE` 候选重新执行 gates，并按现有协议构建相应结果证据；
- 不持有 review 或 signoff 私钥，不作复核和签收决定。

### Dyro 与人类控制面

| 能力                                   | 所有者                     |
| -------------------------------------- | -------------------------- |
| 任务 DAG、人工决策、冲突组、调度快照   | Dyro                       |
| claim、租约、attempt、重试与问题续跑   | Dyro                       |
| task worktree、逐仓 HEAD、clean 检查   | Dyro                       |
| 任务内部 pipeline、分支与语义结果汇总  | Workflow Sandbox           |
| Agent 调用、并发、单次超时与供应商凭证 | Agent Broker               |
| bundle、输入、最终结果和产物验证       | 可信 Supervisor / Packager |
| 确定性 gates 与 gate 日志              | Dyro 证据构建流程          |
| receipt、provenance 与 execution 签名  | Supervisor + Dyro          |
| 独立 review、signoff 与各自签名        | 不同的人类主体和不同私钥   |
| merge、push、Change Set 与审计 Witness | Dyro                       |

execution、review 和 signoff 不只使用不同 `purpose`，还必须由不同主体持有不同 key fingerprint。任务创建与 evidence 导入时都要验证该独立性，不能只信任 receipt 中的 reviewer 字符串。

## 可复现身份与供应链

单个入口 Workflow 文件的 SHA-256 不能代表完整执行物。PoC 必须生成不可变 bundle manifest，至少包含：

- 外部语义运行时 包名、精确版本、包 tarball integrity 与来源；
- 项目本地 lockfile 及其 SHA-256，以及全部传递依赖的名称、版本和 integrity；安装必须使用 frozen lockfile；
- Workflow 入口及全部本地静态/动态导入文件的路径、大小和 SHA-256；
- Agent Broker 与 Supervisor wrapper 的文件清单和 SHA-256；
- Bun、Agent CLI、操作系统镜像或包含已安装依赖的最终执行容器 digest；
- Workflow schema、结果 schema 和脱敏策略版本；
- canonical input 的 RFC 8785 JCS SHA-256。

manifest 使用确定性序列化后计算根哈希。执行前和打包前各验证一次；任何文件缺失、额外文件、哈希变化、依赖重新解析、完整性不匹配或未声明动态导入都必须 fail-closed。

禁止依赖全局安装、浮动版本、运行时 `bun install` 或从网络创建 Workflow。若不能完整绑定 TypeScript/JavaScript 依赖闭包，PoC 不得通过。

## 最终结果协议

Workflow 必须写出符合固定 JSON Schema 的结果 envelope。最小语义为：

```json
{
  "schema_version": 1,
  "status": "DONE",
  "workflow_run_id": "01J...",
  "branches": [
    {
      "id": "analysis-a",
      "critical": true,
      "status": "success",
      "error_code": ""
    }
  ],
  "artifacts": [
    {
      "repository": "docs",
      "path": "report.md",
      "sha256": "..."
    }
  ],
  "question": ""
}
```

Supervisor 必须独立验证：

- envelope 是唯一、完整且符合 schema 的最终结果；
- `status` 只能是 `DONE`、`BLOCKED` 或 `QUESTION`；
- `DONE` 时所有 critical branch 均明确为 `success`；
- 不接受缺失分支、`null`、未知状态、重复分支 ID 或未声明分支；
- 产物 repository 在任务清单中，path 是相对路径且无穿越；
- realpath 位于对应 worktree 内，路径链和最终文件均不是符号链接；
- 产物是允许的普通文件，大小在上限内，重新计算的 SHA-256 匹配。

自由文本 Markdown 仅是产物，不能驱动 `DONE`。进程退出码为零也不能覆盖结果协议失败。

## 事件与证据

当前外部证据包只允许既定文件集合，不能为了 PoC 绕过白名单加入任意事件文件。

语义运行时的 Workflow 事件主要覆盖 Workflow 生命周期和显式日志；供应商 CLI 的内部 stdout/stderr 不等同于该事件流。PoC 分开定义三类数据：

- Workflow 事件：phase、step 与显式 log；
- Broker 遥测：Agent 调用状态、duration、退出原因、截断和资源计数；
- 供应商原始输出：只存在于 Broker 的短生命周期隔离临时文件系统。

过渡规则如下：

- Workflow 事件和 Broker 遥测在进入采集文件前流式脱敏；
- 采集目录权限不得宽于 `0700`，文件不得宽于 `0600`，最长保留 24 小时；
- Supervisor 在 evidence build 前重新计算采集文件 SHA-256，并把文件名、字节数、哈希、截断状态和策略版本写入 `receipt.md`；
- `receipt.md` 只内嵌有大小上限的脱敏摘要；
- Dyro 现有导入只绑定 receipt，不会在导入后重新读取或验证外部事件文件；因此不得声称这些文件是 Dyro 托管的不可变证据；
- 外部事件文件在打包前被篡改必须由 Supervisor 拒绝；打包完成后对外部文件的修改不影响既有 evidence，审计只能依赖 receipt 中的哈希；
- 如果完整事件流必须成为长期可验证证据，应另立 ADR，设计带版本、大小和脱敏规则的 evidence artifact schema。

由于上游 Codex adapter 会在临时文件中保存原始最终输出，“永不落盘”不是当前可证明承诺。PoC 的承诺是：未脱敏内容不得写入宿主持久目录，不得离开 Broker 的短生命周期隔离临时文件系统，并在每次调用结束后销毁。

## Claim、租约与故障恢复

Supervisor 启动前必须取得与 runner ID、execution key ID 和 generation 绑定的 claim 文件，并把它作为 `dyro task evidence build --claim ...` 的唯一 claim 输入。

租约必须覆盖最坏情况下的 Workflow 总 deadline、gates、打包和导入窗口，并预留安全余量。若平台允许续租，只能由可信 Supervisor 在半衰期前续租；Workflow Sandbox 与 Agent Broker 无权续租。租约过期、generation 改变或续租失败后，Supervisor 必须取消运行并拒绝构建 evidence。

## 安全要求

- 所有子进程必须使用 argv 方式启动，禁止拼接 shell 字符串。
- Workflow 输入使用只读文件或 stdin 传递，不把任意用户内容插入命令文本。
- 默认拒绝 `danger-full-access`，额外可写目录必须显式列出。
- 必须设置 Workflow 总 deadline 和单 Agent deadline，并在超时后终止完整进程树。
- 必须在启动前限制输入规模、产物规模和最大并行分支数。
- Broker 必须用 semaphore 控制真实 Agent 并发，不能只检查输入分支数量。
- 环境、JSONL、stdout、stderr、结果和 receipt 写入前必须执行 allowlist 与脱敏。
- Workflow 和 Agent 运行时中不得出现 execution/review/signoff key、Dyro CLI 或 Git 凭证。
- 任一关键分支为 `null`、缺少结构化结果、进程非零退出、超时、取消或 Broker 失联时，Supervisor 必须 fail-closed。
- Workflow 产出 `DONE` 候选后，仍由 Dyro 证据构建流程重新验证分支、clean 状态、逐仓 HEAD 和所有声明 gates。

## 被否决的方案

### 用 外部语义运行时 替换 Dyro TaskGraph

否决。外部语义运行时 的 code-first 控制流不提供跨时段的持久调度、依赖集成检查、冲突组 claim 或交付状态机。

### 把 外部语义运行时 加入 Dyro Core 依赖

否决。这样会让 Python Core 强制依赖 Bun、TypeScript 和特定 Agent CLI，并扩大安装、供应链和故障边界。

### 在同一 Runner 进程中执行 Workflow 和签名

否决。外部语义运行时 通过普通 TypeScript dynamic import 执行 Workflow，默认 Agent 还会继承进程环境；同进程模型无法可靠隔离 Dyro CLI、Git 凭证和 execution key。

### 把 外部语义运行时 输出当作 gate

否决。Agent 生成结果是语义输出，不是可重复执行的确定性验证。它可以形成回执内容，但不能证明 gates 通过。

### 运行时创建 Workflow 后立即执行

否决。动态创建的 Workflow 没有经过 bundle 审核、依赖闭包绑定和 allowlist，不能进入受控执行链。

### 只复制 外部语义运行时 的实现

暂不采用。phase、JSONL 事件和 Agent 接口可以作为设计参考；如复制 MIT 代码，必须保留适用的版权和许可证声明，并先证明维护自有分叉的收益。

## 影响

正面影响：

- 可以复用 TypeScript Agent 编排和结构化输出能力。
- Dyro 无需承担任务内部每一种语义工作流 DSL。
- 可选集成可被完整移除，不需要迁移 Dyro 状态或证据。

代价与风险：

- 外部执行链需要维护 Python/Dyro、Bun/TypeScript、Broker 和隔离环境。
- 外部语义运行时 当前仍处于早期版本，API 和事件协议可能变化。
- 三个信任域增加部署和观测成本；如果不能形成真实安全隔离，PoC 没有成立条件。
- 双层工作流容易混淆所有权，因此日志、错误和文档必须明确标注 Dyro task、Workflow run 和 Agent invocation identity。
- 当前证据 schema 不保存完整 Workflow 事件流；PoC 只能验证 receipt 哈希摘要方案是否足够。

## 重新评估条件

出现以下任一情况时重新审阅本 ADR：

- PoC 需要修改 Dyro Core 调度或状态语义才能工作；
- 外部语义运行时 增加持久 checkpoint、队列或与 Dyro 重叠的交付控制；
- 完整事件流成为审计强制证据；
- Bun/TypeScript 运行时进入 Dyro 的其他核心能力；
- Agent Broker 无法在不暴露宿主凭证的情况下兼容所需供应商；
- PoC 完成后准备产品化；三次相同规格运行只能证明 PoC 稳定性，不能单独证明生产就绪。
