# 外部语义运行时 外部 Runner PoC

## 目标

验证 外部语义运行时 能否作为 Dyro 外部执行链中的可选语义工作流运行时，同时保持 Dyro 的任务、Git、gates、证据、复核与合并边界不变。

本 PoC 依据 [ADR-0001](adr/0001-optional-external-semantic-runtime.md) 执行。它验证集成和安全隔离是否可行，不承诺产品化。

## 非目标

- 不替换 Dyro TaskGraph、scheduler、daemon 或状态机。
- 不把 Bun、TypeScript 或 外部语义运行时 加入 Dyro Core 依赖。
- 不修改 `done`、review、signoff、merge 或 push 规则。
- 不把 Agent 输出改名为 gate evidence。
- 不在首个 PoC 中扩展外部证据包文件白名单。
- 不实现通用 Workflow 市场、远程 SaaS 或可视化编辑器。
- 不执行任意用户提供、运行时生成或未复核的 Workflow。
- 不以三次成功运行证明生产就绪。

## 试点任务

选择一个低风险、可回滚的文档型任务：

1. 输入包含两个或以上本地仓库的分析目标。
2. 外部语义运行时 并行执行只读分析分支。
3. 汇总分支生成一份 Markdown 报告到指定任务仓库。
4. Supervisor 验证结果和产物，提交报告并保证所有任务 worktree clean。
5. Dyro 独立执行确定性的格式或内容 gates。
6. 独立 reviewer 根据 receipt、attempt 和逐仓 HEAD 作出裁决。

不得选择生产 Hotfix、数据库迁移、凭证轮换、发布或自动 push 作为首个试点。

## 前置策略

试点 Profile 使用最严格的现有外部执行链：

```toml
[policy]
execution_mode = "external"
allow_push = false
require_signed_execution = true
require_signed_review = true
require_external_signoff = true
require_signed_signoff = true
```

task manifest 显式关闭自动交付：

```toml
[merge]
auto = false
push = false
```

execution、review 和 signoff 使用不同主体持有的不同 key fingerprint，并各自限制为对应 `purpose`。PoC 在 claim 前和 evidence 导入前执行独立性预检；主体或 fingerprint 相同即停止。该预检是 PoC 的附加控制，不能误称为当前 Dyro Core 已原生强制。

所有私钥存放在工作区和 Workflow 隔离域外。Workflow Sandbox 与 Agent Broker 的环境、文件系统、输入和日志中不得出现 execution、review 或 signoff 私钥。

## 固定上游候选

首个 PoC 只使用已发布包：

```text
package: @dyro/semantic-flow
version: 0.2.0
npm dist.integrity:
  sha512-sJgf79AHIwx67b570lMOuQjpouXepXSlfTeLXNobEubYzcViQZslnqRw2XEvYjF9+N3VUlpy6ID5qziSS1ICBw==
same-version source tag reference: v0.2.0
source tag peeled commit:
  73c61156197445be4a0fad390e3a1d802f2cda4a
```

评估时上游 `main` 为 `4cf5f2fc6804f0c65807023e00133a017197c920`，包含 `v0.2.0` 之后的提交。它不是本 PoC 的已发布包身份，也不能与 `0.2.0` 混写。

安装必须在 PoC 项目本地使用 frozen lockfile 完成，禁止全局安装和浮动解析。执行环境不得在运行时执行 `bun install`、下载新代码或更新 lockfile。

如必须切换到源码 checkout：

1. 将其作为新的 PoC 候选；
2. 固定完整 Git commit，不再称为 npm `0.2.0` 发布物；
3. 重新生成依赖闭包、bundle manifest 和威胁复核；
4. 重新运行全部上游门禁和本 PoC 验收项。

## 三个信任域

### 1. Workflow Sandbox

Workflow Sandbox 运行固定 外部语义运行时 bundle，并按不可信代码处理：

- 根文件系统与 Workflow bundle 只读；
- 只挂载任务允许的 worktree 为可写；
- `/tmp` 使用短生命周期隔离文件系统；
- 默认无网络，只允许连接本地 Agent Broker IPC；
- 使用环境变量 allowlist，不继承 Supervisor 环境；
- 环境和 `PATH` 中没有 Dyro CLI、Git 凭证、供应商凭证和签名私钥；
- 禁止运行时 Workflow 创建、依赖安装和 bundle 外动态 import；
- 启动时应用总 deadline、内存、进程数、输出和文件大小上限。

恶意 Workflow 在模块顶层执行写文件、读私钥、启动后台进程或访问网络，也必须受相同隔离约束，不能只约束 `agent()` 调用。

### 2. Agent Broker

Agent Broker 是独立进程或服务，通过窄 IPC 实现 语义运行时 `Agent` 接口：

- 只接收固定 schema 的调用 ID、prompt 引用、模型、工作目录引用和 deadline；
- 只持有调用供应商所需的最小凭证；
- 使用 semaphore 强制真实最大并发；
- 对每次 Agent 调用强制 deadline、输入/输出大小和模型 allowlist；
- 使用 argv 启动供应商 CLI，不执行拼接后的 shell 命令；
- 供应商 CLI 使用独立短生命周期临时文件系统；
- 只向 Sandbox 返回通过 schema 验证和脱敏的数据；
- 取消或超时时终止完整后代进程树，并确认无遗留进程；
- 不持有 Dyro CLI、Git 凭证或任何 Dyro 签名私钥。

上游 Codex adapter 会把原始最终输出写入临时文件，因此 PoC 不承诺“原始输出从不落盘”。可验证承诺是：未脱敏内容不进入宿主持久目录，不离开 Broker 的短生命周期隔离临时文件系统，并在每次调用结束后销毁。

### 3. 可信 Supervisor / Packager

Supervisor 不 import 或执行 Workflow 代码。它负责：

- 校验 task、claim、租约、bundle manifest、canonical input 和运行身份；
- 创建并监督 Sandbox 与 Broker；
- 生成 `workflow_run_id`，并要求最终结果原样绑定；
- 接收、验证最终结果 envelope 和声明产物；
- 重新计算产物、事件与 Broker 遥测的哈希；
- 关闭 Sandbox 与 Broker，并确认没有遗留进程；
- 在此之后临时挂载 execution key；
- 提交允许的产物；
- 使用 claim 文件调用 `dyro task evidence build --claim ...`；
- 卸载 execution key，交由 Dyro 控制面导入、review 和 signoff。

Supervisor 不持有 review 或 signoff 私钥，也不执行 merge 或 push。

## Claim 与租约

控制面签发的 claim 文件必须绑定：

- task ID；
- runner ID；
- generation；
- execution key ID；
- 签发与到期时间。

Supervisor 将该原始 claim 文件传给 `dyro task evidence build --claim ...`，Workflow Sandbox 和 Agent Broker 不得读取、修改或续租 claim。

租约配置必须覆盖：

```text
Workflow 总 deadline
+ Sandbox/Broker 清理窗口
+ gates 最坏耗时
+ 产物提交与证据打包窗口
+ evidence 导入窗口
+ 安全余量
```

如果控制面支持续租，只能由 Supervisor 在租约半衰期前续租。租约到期、generation 改变、runner 不匹配或续租失败时，Supervisor 立即取消运行并拒绝构建 evidence。PoC 必须记录续租请求、响应和最终使用的 generation。

## Bundle manifest

单个 Workflow 入口文件的 SHA-256 不足以绑定实际执行代码。构建阶段必须创建确定性 `bundle-manifest.json`，至少记录：

- manifest schema version；
- 外部语义运行时 包名、精确版本、tarball integrity 和来源；
- lockfile 路径与 SHA-256，以及全部传递依赖的名称、版本和 integrity；
- Workflow 入口和全部本地导入的相对路径、字节数、SHA-256；
- Agent Broker 与 Supervisor wrapper 的相对路径、字节数、SHA-256；
- 结果 JSON Schema 和脱敏规则文件的 SHA-256；
- Bun、Agent CLI 的精确版本；
- 操作系统镜像或包含已安装依赖的最终执行容器 digest；
- 构建工具版本。

manifest 使用确定性 JSON 序列化后计算 `bundle_manifest_sha256`。Supervisor 在 Sandbox 启动前和 evidence build 前分别验证：

- manifest 内所有路径都在 bundle 根目录；
- realpath 未逃逸，路径链和文件均不是符号链接；
- 文件集合完全一致，没有缺失或额外文件；
- 所有大小和 SHA-256 匹配；
- 包完整性、lockfile 和运行时身份匹配；
- bundle 挂载为只读。

任何 transitive import、wrapper、schema、lockfile 或运行时身份改变都必须生成新的根哈希并重新复核。无法静态闭合的动态 import 必须由显式 allowlist 列入 manifest，否则 PoC fail-closed。

## 输入协议

PoC 开始前固定并记录：

- Dyro 版本和 Git commit；
- `bundle_manifest_sha256`；
- canonical input 的 RFC 8785 JCS SHA-256；
- task ID、line、claim ID、generation 和 runner ID；
- 允许访问的仓库、worktree、产物路径与环境变量名；
- 最大并行分支、Workflow 总 deadline、单 Agent deadline；
- 单次输出、总事件、产物数量和产物字节数上限；
- 脱敏策略版本。

Supervisor 把 canonical input 作为只读文件挂载。不得把任意用户内容拼入 shell、JSON 字符串模板或环境变量；Workflow 只能通过固定解析器读取。

## 最终结果协议

Workflow 必须写出唯一的 `result-envelope.json`。PoC 固定 JSON Schema，语义示例如下：

```json
{
  "schema_version": 1,
  "status": "DONE",
  "workflow_run_id": "01J...",
  "branches": [
    {
      "id": "repo-a-analysis",
      "critical": true,
      "status": "success",
      "error_code": ""
    },
    {
      "id": "repo-b-analysis",
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

固定规则：

- `status` 只能是 `DONE`、`BLOCKED` 或 `QUESTION`；
- `workflow_run_id` 必须与 Supervisor 生成值一致；
- 分支 ID 必须来自输入清单且唯一；
- `DONE` 要求所有 critical branch 明确为 `success`；
- `failed`、`question`、`null`、缺失或未知 critical branch 均不得映射为 `DONE`；
- `BLOCKED` 必须至少包含一个明确失败或不可恢复原因；
- `QUESTION` 必须包含非空、脱敏且限长的 `question`；
- artifact repository 与 path 必须来自输入 allowlist；
- artifact path 必须是相对路径，不含 `..`、空段、NUL 或平台歧义；
- artifact realpath 必须位于对应 worktree，路径链和最终文件均不得是符号链接；
- artifact 必须是允许类型的普通文件，数量和大小在上限内；
- Supervisor 重新计算的 SHA-256 必须与 envelope 一致。

进程退出码为零、Markdown 汇总存在、语义运行时 `parallel()` 返回部分结果或 Workflow 自述成功，都不能覆盖 schema 或 critical branch 失败。结果 envelope 缺失、重复、截断或解析失败时 fail-closed。

## 执行流程

```text
 1. 控制面创建任务、worktree 并签发 claim
 2. Supervisor 验证 claim、租约、主体和 key fingerprint 独立性
 3. Supervisor 验证 bundle manifest 与 canonical input
 4. Supervisor 启动隔离的 Agent Broker
 5. Supervisor 启动无凭证、无 Dyro CLI 的 Workflow Sandbox
 6. Sandbox 通过 Broker 在并发和 deadline 下执行固定 Workflow
 7. Supervisor 接收并验证 result envelope、关键分支和产物
 8. Supervisor 终止 Sandbox/Broker，验证无遗留后代进程
 9. Supervisor 重新验证 bundle、claim、租约、worktree 和所有哈希
10. Supervisor 临时挂载 execution key，提交允许产物
11. dyro task evidence build --claim ... 按结果构建证据；仅 DONE 候选重新执行 gates 并记录 HEAD
12. Supervisor 卸载 execution key，控制面导入 evidence
13. 独立 reviewer 复核，独立 approver 按策略 signoff
14. 仅由 Dyro 执行显式 merge；PoC 默认不 push
```

Workflow Sandbox 与 Agent Broker 不得写 `.dyro/` 控制面状态。Supervisor 仅通过现有 Dyro 命令和 claim 契约写入受控证据，不直接修改 `.dyro/`。

## 事件、遥测与回执

外部语义运行时 的 JSONL 主要覆盖 Workflow 生命周期与显式日志，不代表供应商 CLI 的完整内部 stdout/stderr。PoC 分开采集：

- `workflow-events.jsonl`：phase、step 与显式 log；
- `broker-telemetry.jsonl`：调用 ID、状态、duration、退出原因、截断和资源计数；
- 供应商原始输出：仅存在于 Broker 的短生命周期隔离临时文件系统，不进入采集目录。

事件与遥测必须在进入采集文件前流式脱敏。隔离采集目录权限不得宽于 `0700`，文件不得宽于 `0600`，最长保留 24 小时。脱敏器异常、二进制输出、超限或清理失败都必须使运行进入 `BLOCKED`，不能静默降级。

Supervisor 在 evidence build 前重新计算采集文件的字节数与 SHA-256，并写入 `receipt.md`。PoC 内嵌脱敏事件摘要的总上限为 1 MiB，超出时明确记录截断、原始脱敏文件总字节数和哈希。

现有 Dyro evidence 不导入这些外部采集文件，也不会在 evidence 构建完成后验证其内容。正确能力边界是：

- evidence build 前篡改采集文件：Supervisor 重算哈希并拒绝；
- evidence build 后篡改或删除外部采集文件：既有 evidence 不变，只能从 receipt 看出原文件哈希，不能由 Dyro 证明其后续内容；
- 篡改 evidence 内的 receipt：现有签名或导入校验拒绝。

不得把外部采集文件描述为 Dyro 托管的不可变证据。若产品要求长期验证完整事件，必须另立 evidence schema ADR。

`receipt.md` 至少记录：

- 首行使用现有协议允许的 `result: DONE`、`result: BLOCKED` 或 `result: QUESTION`；
- task ID、claim ID、generation、runner ID 和 `workflow_run_id`；
- `bundle_manifest_sha256`、外部语义运行时 package version 和 integrity；
- canonical input SHA-256，不记录敏感明文输入；
- Bun、Agent runtime、版本和模型标识；无法取得时明确写 `unknown`；
- 开始、结束时间和 duration；
- 配置并发、观测到的最大并发、总 deadline 与单 Agent deadline；
- 每个 critical branch 的明确状态和 error code；
- 每个产物的 repository、path、字节数和重新计算的 SHA-256；
- Workflow 事件与 Broker 遥测的字节数、SHA-256、截断状态和策略版本；
- 隔离清理与遗留进程检查结果；
- 任务提交摘要；
- 未验证、跳过或降级的能力。

Dyro external `run_id` 和 `attempt_id` 由 `dyro task evidence build` 写入 `provenance.json`，生成 receipt 时尚不存在，不能伪造或预填。

## 验收标准

所有必选项通过后，PoC 才能判定“隔离集成可行”。

| ID     | 必选验收项    | 通过条件                                                                                                                |
| ------ | ------------- | ----------------------------------------------------------------------------------------------------------------------- |
| POC-01 | 控制面所有权  | Dyro 仍是 task status、claim、attempt、review 和 merge 的唯一写入者。                                                   |
| POC-02 | 可移除性      | 删除 Sandbox、Broker、Supervisor、Workflow 和私有 Profile 后，Dyro Core 安装与现有测试不受影响。                        |
| POC-03 | 发布物身份    | 使用精确 npm 版本与 integrity；`v0.2.0` 提交和评估 `main` 提交不混用。                                                  |
| POC-04 | 完整依赖闭包  | bundle manifest 覆盖包、lockfile、全部导入、wrapper、schema、运行时与镜像；任一变化都会改变根哈希或失败。               |
| POC-05 | Workflow 隔离 | 模块顶层恶意代码也只能写允许 worktree/tmpfs，不能访问宿主网络、凭证、Dyro CLI 或 bundle 外文件。                        |
| POC-06 | 凭证隔离      | Sandbox 无供应商凭证；Sandbox/Broker 均无 Git 凭证和 execution/review/signoff key；签名仅在清理后的 Packager 阶段挂载。 |
| POC-07 | Broker 并发   | semaphore 强制实际 Agent 并发不超过配置值，排队调用仍受 deadline 约束。                                                 |
| POC-08 | 单 Agent 超时 | deadline 后终止供应商 CLI 的完整后代进程树，没有孤儿进程和继续写入。                                                    |
| POC-09 | Workflow 超时 | 总 deadline 后终止 Sandbox、Broker 和全部后代，且不构建 `DONE` evidence。                                               |
| POC-10 | 结构化结果    | 唯一 envelope 通过 schema；任何 critical branch 的 `null`、失败、缺失、重复或未知状态均不能映射为 `DONE`。              |
| POC-11 | 产物边界      | repository/path allowlist、realpath、无 symlink、类型、数量、大小和 SHA-256 全部通过才允许提交。                        |
| POC-12 | 确定性 gates  | `DONE` 候选的所有 task gates 由 `dyro task evidence build` 重新执行，Agent 自述不计入 gate。                            |
| POC-13 | Git 绑定      | evidence build 前任务分支正确、worktree clean，逐仓 HEAD 与提交产物一致。                                               |
| POC-14 | 证据绑定      | receipt、gates、task-heads 和 provenance 能通过现有导入校验与签名策略。                                                 |
| POC-15 | 独立主体      | execution、review、signoff 的主体和 key fingerprint 各不相同，且 review 绑定当前 receipt、attempt、plan 和 HEAD。       |
| POC-16 | 无越权交付    | Sandbox、Broker、Supervisor 均不执行 review、signoff、merge、push 或强制状态转换。                                      |
| POC-17 | 原始输出边界  | 未脱敏供应商输出只存在于 Broker 短生命周期临时文件系统，调用结束后销毁，不进入宿主持久目录、receipt 或导入 evidence。   |
| POC-18 | 日志安全      | token、私钥样例和敏感输入不会进入脱敏采集文件或 receipt；权限、上限、24 小时清理和失败关闭策略生效。                    |
| POC-19 | 事件能力声明  | Supervisor 可阻止打包前采集文件篡改；测试与文档不声称 Dyro 会验证打包后外部事件文件。                                   |
| POC-20 | Claim/租约    | claim 文件完整传递；generation/runner/key 不匹配、过期或续租失败会取消运行并拒绝 evidence。                             |
| POC-21 | 重放安全      | 同一规范化 evidence 可幂等重试；修改 receipt、HEAD 或 provenance 后导入失败。                                           |
| POC-22 | 故障恢复      | Sandbox 崩溃、Broker 失联、Agent 失败、gate 失败和清理失败均不会产生错误 PASS 或 `done`。                               |
| POC-23 | 回归门禁      | 固定语义运行时发布物的官方测试与 Dyro 官方测试均通过，现有行为无回归。                                                      |
| POC-24 | 资源上限      | 输入、输出、事件、产物、进程和内存超限均 fail-closed，错误可归因且不泄露敏感内容。                                      |

## 测试矩阵

| 场景                                          | 预期结果                                                    |
| --------------------------------------------- | ----------------------------------------------------------- |
| 两个只读分析分支成功并汇总                    | 生成 schema 合法结果和报告，gates 通过后进入 review。       |
| 一个关键分支返回 `null`                       | 结果验证失败或生成 `BLOCKED`，绝不生成 `DONE`。             |
| Workflow 过滤失败分支后进程返回零             | Supervisor 仍根据预期分支清单拒绝 `DONE`。                  |
| 结果缺失、重复、截断或包含未知状态            | fail-closed，不提交产物或 evidence。                        |
| 分支数量超过输入上限                          | Sandbox 启动前失败，没有 Agent 调用。                       |
| 多分支同时请求 Agent                          | Broker 观测到的实际并发始终不超过 semaphore 上限。          |
| Agent 超过单次 deadline                       | 供应商 CLI 和全部后代被终止，attempt 不成功。               |
| Workflow 超过总 deadline                      | Sandbox、Broker 与全部后代被终止，任务不进入 review。       |
| Agent 忽略终止并派生后台进程                  | Supervisor 检测并清理孤儿进程，清理验证失败时不签名。       |
| Workflow 在模块顶层写未授权目录               | 隔离层拒绝，证明约束覆盖普通 TypeScript，而不只覆盖 Agent。 |
| Workflow 尝试读取环境或签名 key               | 变量/文件不存在，测试哨兵不得出现在输出。                   |
| Workflow 尝试网络访问或直接运行 Agent CLI     | 网络和可执行文件策略拒绝，只允许 Broker IPC。               |
| Workflow 产物使用 `..`、绝对路径或 symlink    | Supervisor 在提交前拒绝。                                   |
| Workflow 完成后 worktree dirty                | evidence build 拒绝。                                       |
| package tarball integrity 或 lockfile 不匹配  | Sandbox 启动前失败。                                        |
| 修改 transitive import、wrapper 或结果 schema | bundle manifest 校验失败或根哈希变化，必须重新复核。        |
| 使用 `0.2.0` 版本号配 `4cf5f2f` 源码身份      | 身份预检失败。                                              |
| Workflow 运行中 claim 过期或 generation 改变  | Supervisor 取消运行并拒绝构建 evidence。                    |
| 租约接近半衰期且续租成功                      | 只由 Supervisor 续租，记录新到期时间后继续。                |
| execution 与 reviewer fingerprint 相同        | claim 前或导入前独立性预检失败。                            |
| 外部采集文件在 evidence build 前被篡改        | Supervisor 重算哈希失败并拒绝打包。                         |
| 外部采集文件在 evidence build 后被篡改        | 既有 evidence 不变；测试明确不期待 Dyro 验证外部文件。      |
| receipt、HEAD 或 provenance 被篡改            | 现有 evidence 导入或 review binding 失败。                  |
| gate 输出 FAIL                                | 不因 Workflow 成功而进入 review。                           |
| Broker 脱敏器、清理或 IPC 失败                | 运行进入 `BLOCKED` 或不构建 evidence，不静默降级。          |
| 重复导入完全相同的 evidence                   | 幂等成功，不创建不同语义的 attempt。                        |

## 验证命令与证据

PoC 实现必须提供一条可重复执行的本地验证入口，完成：

1. bundle 构建与 manifest 校验；
2. 成功、失败、超时、并发、权限和供应链测试；
3. 固定语义运行时发布物的上游测试；
4. Dyro 全量官方测试；
5. 一次端到端外部 evidence 构建、导入和独立 review 演练。

运行报告记录命令、退出码、开始/结束时间、工具版本和脱敏日志路径。不能只写“测试通过”。

## 运行指标

至少连续执行三个相同规格的试点，并记录：

- 成功率和每次失败原因；
- 总耗时、排队时间、Agent 耗时和 gates 耗时；
- Agent 调用次数、最大实际并发与超时数量；
- token 或供应商成本；无法取得时明确标记未知；
- Workflow 事件、Broker 遥测和 evidence bundle 大小；
- 人工处理次数；
- 相比单 Agent adapter 的额外实现和运维成本。

这些指标只用于判断 PoC 稳定性和工程收益，不作为绕过安全验收项或证明生产就绪的理由。

## 停止条件

出现以下任一情况立即停止 PoC：

- 必须修改 Dyro 调度或状态机才能表达 Workflow 内部阶段；
- 无法把 Workflow 普通 TypeScript 执行与宿主凭证、文件系统、网络隔离；
- 无法让关键 `null`、部分结果或成功退出稳定映射为失败；
- 无法通过 Broker 限制并发或终止完整子进程树；
- 需要赋予 `danger-full-access` 才能完成试点；
- 无法绑定完整依赖闭包、wrapper、schema 和运行时身份；
- 无法在 Sandbox/Broker 销毁前阻止 execution key 暴露；
- 产物 realpath 或 symlink 边界无法可靠验证；
- claim 租约无法覆盖运行、打包和导入，且不能安全续租；
- 完整日志必须进入当前 evidence bundle，但无法在不破坏兼容性的情况下实现；
- 日志脱敏或短生命周期原始输出清理测试失败；
- execution、review 和 signoff 无法由不同主体与不同 key fingerprint 承担；
- 任何故障注入产生错误 PASS、review 或 `done`。

## PoC 交付物

进入实现阶段后应产生：

1. 独立的 Supervisor / Packager，不放入 Dyro Core；
2. 隔离的 Agent Broker 与窄 IPC schema；
3. Workflow Sandbox 配置和恶意 Workflow 测试夹具；
4. 固定发布物、frozen lockfile 和只读 bundle；
5. `bundle-manifest.json` 及生成/验证工具；
6. 固定的结果 JSON Schema 与示例 Workflow；
7. 私有或 examples 范围的 Profile；
8. 成功、失败、超时、并发、权限、供应链和租约测试；
9. 脱敏的示例 receipt、Workflow 事件与 Broker 遥测；
10. 三次连续运行、故障注入和运行成本报告；
11. 产品化、继续观察或放弃的结论。

## PoC 完成门槛

只有同时满足以下条件，PoC 才能结论为“隔离集成可行”：

- `POC-01` 至 `POC-24` 全部通过；
- 三次相同规格试点无错误 PASS；
- 测试矩阵中的故障注入全部通过；
- 没有新增 Dyro Core 运行时依赖或改变状态语义；
- 所有未验证能力被明确列出，且没有未关闭的高风险项；
- 维护者确认编排收益足以继续扩大试点。

## 产品化前置条件

PoC 完成不等于生产就绪。提出产品化 ADR 前还必须：

- 在多种真实但非生产关键的任务类型上完成更长周期试点；
- 完成独立安全威胁建模和 Sandbox/Broker 逃逸评估；
- 定义部署、升级、撤销、密钥轮换、容量、告警和事故响应；
- 确认上游版本维护策略、漏洞响应和依赖更新 SLA；
- 决定完整事件流采用外部存档、receipt 摘要或新 evidence schema；
- 对关键组件建立版本兼容矩阵和回滚演练；
- 由新的 ADR 明确接受剩余风险和生产所有者。

未达到 PoC 门槛时，删除可选 Sandbox、Broker、Supervisor、Workflow 和 Profile 即完成回滚；Dyro 不需要迁移任何状态。
