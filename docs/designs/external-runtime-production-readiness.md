# 外部语义运行时生产就绪与用户体验设计

## 产品目标

让 Dyro 用户能清楚回答四个问题：

1. 这台机器能否运行本地 Stage5？
2. 当前为什么不能上线？
3. 一个 runtime 结果怎样安全进入 Dyro Core？
4. 下一步由谁完成、用什么证据验收？

产品不以“命令执行成功”冒充“生产门禁通过”，也不把部署环境责任隐藏在
模糊的 `experimental` 标签后面。

## 用户与职责

| 用户 | 负责 | 不负责 |
| --- | --- | --- |
| 开发者 | 定义 Task、gates、固定 workflow 输入 | 放行生产环境 |
| Runner operator | 安全传递 claim、运行 Stage5、构建签名 Core bundle | review、signoff、merge、push |
| Independent reviewer | 核对 receipt、HEAD、attempt、plan 与风险发现 | 执行任务或修改源码 |
| Release manager | 核对全部生产证据并 signoff | 替 runtime 补造证据 |

## 首次体验

### 1. 先看状态

```bash
dyro runtime status
```

交互终端显示简洁摘要；重定向或管道默认输出稳定 JSON。也可显式使用
`--human` 或 `--json`。

### 2. 检查本机

```bash
dyro runtime doctor
dyro runtime doctor \
  --provider-path /opt/dyro/providers/codex \
  --provider-root /opt/dyro/providers
```

Doctor 不拉取镜像、不写工作区、不调用 provider。输出必须明确区分：

- 本地 PoC 环境是否可用；
- 生产是否就绪（当前始终由 production gate 决定）；
- 每个失败项的修复动作。

### 3. 查看生产晋级计划

```bash
dyro runtime plan
```

计划只读，展示已完成、受阻、待执行和需本机检查的阶段。环境阻断项不能通过
修改本地 JSON 清除。

## 受控执行证据旅程

### 1. Core 领取并安全导出 claim

```bash
dyro task claim TASK-42 \
  --by stage5-runner-01 \
  --key-id runner-2026 \
  --output /secure-transfer/TASK-42.core-claim.json
```

输出文件权限为 `0600` 且拒绝覆盖。已有输出路径会在任务领取前报错，避免
产生“任务已领取但文件没导出”的常见误解。Runner 读取时也会拒绝符号链接、
非普通文件、超过 64 KiB 的文件以及任何 group/world 权限，跨边界复制不能
放宽权限。

### 2. Runner 缩减 claim 权限

```bash
dyro runtime claim prepare \
  --core-claim /runner/inbox/TASK-42.core-claim.json \
  --output /runner/state/TASK-42.stage5-claim.json
```

使用 `dyro --dry-run runtime claim prepare …` 可以只验证。Stage5 的任何
续租都不能晚于 Core claim 到期时间。

### 3. 运行固定 Stage5 workflow

当前 Stage5 Supervisor 是供部署适配层调用的固定 API，不接受任意用户
workflow。部署适配层必须把上一步 claim、钉扎 provider、明确 worktree、
artifact allowlist 和资源上限传给 `Stage5SupervisorConfig`。

在真实 provider 舰队和凭据挂载契约完成前，Dyro 不提供一个容易让用户误以为
“可直接生产运行”的宽泛 `runtime run` 命令。该限制属于 `PROD-02`，不是
文档遗漏。

### 4. 构建 Core execution bundle

Stage5 完成、Sandbox/Broker 双重清理、artifact 提交且工作区干净后：

```bash
dyro runtime handoff \
  --root /runner/dyro-profile \
  --task TASK-42 \
  --pack /runner/state/TASK-42/stage5-pack \
  --workspace /runner/workspaces/TASK-42 \
  --core-claim /runner/inbox/TASK-42.core-claim.json \
  --output /runner/out/TASK-42.core-evidence.zip \
  --signing-key /secure-keys/runner-2026.pem \
  --key-id runner-2026
```

Handoff 会执行 Core 声明的 gates 并固定干净 HEAD。它只构建 ZIP；全部
gates 通过时，输出的 `next_command` 才会提醒用户回到控制面显式导入。
gate 失败时仍保留签名诊断 bundle，但返回 `BLOCKED`/退出码 3 且不提供导入
命令。`--dry-run` 只验证 claim、pack、artifact 与结构化计划，不执行 gates、
不固定 HEAD、也不创建签名或 ZIP。

私钥必须是权限为 `0600` 的普通非符号链接文件，并位于 Dyro Profile、
runner workspace 与 Stage5 pack 三者之外。此规则在 `--dry-run` 也会检查，
避免把可由 workflow 或仓库内容触达的路径误当作可信密钥挂载。

### 5. Core 导入与独立复核

```bash
dyro task evidence execution TASK-42 \
  --bundle /control/inbox/TASK-42.core-evidence.zip

dyro task binding TASK-42
dyro task evidence review TASK-42 --file /review/out/review.json
dyro task signoff TASK-42 --by release-manager  # Profile 要求时
```

Core import 会重新验证当前 task claim、签名 trust 状态、receipt、gates、HEAD
与 provenance。Runner 侧的 `ready_for_core_import` 只表示 bundle 具备提交
条件；若 claim 已释放/被新 generation 接管或密钥已撤销，Core 仍会拒绝。

Runtime 不执行上述命令，也不能把 bundle 构建成功解释为任务完成。

## 命令与退出码

| 命令 | 成功退出码 | 阻断退出码 | 是否写状态 |
| --- | ---: | ---: | --- |
| `runtime status` | 0 | — | 否 |
| `runtime doctor` | 0 | 3 | 否 |
| `runtime plan` | 0 | — | 否 |
| `runtime production-gate` | 0 (`READY`) | 3 (`NOT_READY`) | 否 |
| `runtime claim prepare` | 0 | 2 | 仅新输出文件 |
| `runtime handoff` | 0 | 3（gate 失败）/ 2（验证错误） | 仅新 Core ZIP；不导入 |

命令格式或验证错误返回 2。任何写入型命令都拒绝覆盖已有输出，并支持顶层
`--dry-run`。

## 失败体验要求

- 错误必须指出失败对象、原因和恢复动作，不能只输出 stack trace。
- `NOT_READY` 是门禁阻断，不是程序崩溃；使用退出码 3。
- Doctor PASS 必须同时显示“生产就绪：否”。
- Claim 过期、代次不符、runner/key 不符时 fail-closed。
- Pack seal、workflow ID、artifact 或 workspace 内容漂移时拒绝 handoff。
- Gate 失败仍可保留诊断 bundle，但不得进入 Core `review` 状态。
- Runtime 输出不得暗示它执行过 import、review、signoff、merge 或 push。

## 当前验收状态

| 范围 | 状态 | 证据 |
| --- | --- | --- |
| 本机三域隔离与双重清理 | 已验证 | Stage0–5 Docker 回归 |
| 生产门禁退出语义与 operator UX | 已实现 | runtime CLI/doctor tests |
| Stage5 claim 受 Core claim 约束 | 已实现 | claim authority/renewal tests |
| Stage5 pack → 签名 Core bundle → independent review | 已实现 | `test_runtime_core_handoff_integration` |
| 多宿主逃逸与租户边界 | 待真实环境 | `PROD-01` |
| 真实 provider/凭据舰队 | 待真实环境 | `PROD-02` |
| 全部可写挂载强制配额 | 待真实环境 | `PROD-09` |

## 上线前最终决策

只有以下条件同时成立才可将 verdict 改为 `READY`：

1. `production-gate` 没有 open blocker 且退出码为 0；
2. wheel 与 sdist 安装后的命令和 runtime bundle 均通过验证；
3. 真实发布环境完成安全、凭据、配额和容量证据；
4. 独立 reviewer 确认 runtime 权限仍不包含交付控制动作；
5. canary、回滚、告警、on-call 与审计保留方案均完成演练。
