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

## 签名生产验收闭环

真实环境整改完成后，发布负责人先在受保护的 Dyro trust root 中登记四个用途
隔离的公钥：

```bash
dyro --root /control/dyro-profile key trust release-2026 \
  --purpose production-release \
  --public-key /trust/release-2026.public.pem
dyro --root /control/dyro-profile key trust security-2026 \
  --purpose production-security \
  --public-key /trust/security-2026.public.pem
dyro --root /control/dyro-profile key trust provider-2026 \
  --purpose production-provider \
  --public-key /trust/provider-2026.public.pem
dyro --root /control/dyro-profile key trust quota-2026 \
  --purpose production-quota \
  --public-key /trust/quota-2026.public.pem
```

四个信任用途必须由不同公钥承担。私钥留在各职责主体的签名系统中；门禁只读取
工作区 trust store 中的公钥。信任目录的权限、密钥审批和 Witness 同步属于部署
控制面责任，不能交给 Sandbox 或 provider。

操作员先从当前安装制品定位或 create-only 导出精确契约：

```bash
dyro runtime production-acceptance schemas --human
dyro runtime production-acceptance schemas \
  --output-dir /release/contracts/dyro-0.5.1
```

随后使用 `release-prepare` 对真实 wheel、sdist、SBOM、provenance、provider
二进制以及 deployment/canary/rollback/observability/runbook 文件做稳定哈希，
并创建未签名发布清单。工具拒绝空文件、符号链接、特殊文件、读取期替换和任何
已有输出；顶层 `--dry-run` 只验证、不写入。完整可复制步骤见
[`生产验收操作员手册`](../production-acceptance-operator-runbook.md)。

未签名发布清单遵循
[`production-deployment-manifest.schema.json`](../../experiments/external_workflow_runner/schemas/production-deployment-manifest.schema.json)
的 signature 之外全部字段；附加已验证 signature 后才成为该 schema 的完整
记录。清单固定：

- Dyro 版本、完整源码 commit 与批准的 Bun 镜像 digest；
- wheel、sdist、SBOM 与 provenance SHA-256；
- 每个真实 provider 二进制 SHA-256；
- deployment、canary、rollback、observability 和 runbook 内容 SHA-256；
- release ID、environment ID 与创建时间。

发布角色使用 `production-release` 签名清单。随后三个独立职责流水线按
[`production-attestation.schema.json`](../../experiments/external_workflow_runner/schemas/production-attestation.schema.json)
分别签署：

| 检查 | 签名用途 | 必须断言 |
| --- | --- | --- |
| `PROD-01` | `production-security` | 多宿主逃逸、租户边界、编排器、内核、存储、网络策略均已验证，开放高危/严重发现为 0 |
| `PROD-02` | `production-provider` | provider 钉扎、Broker-only 凭据、轮换、撤销、恢复均已验证，至少一次真实 canary，开放高危/严重发现为 0 |
| `PROD-09` | `production-quota` | 所有可写挂载已声明并强制字节/inode/文件数上限，耗尽与并发租户测试通过，开放高危/严重发现为 0 |

每份证明由 `attestation-prepare` 基于操作员显式 assertions 和真实 evidence
文件准备。该命令不会生成模板、推断 `pass` 或自动把断言设为真。每份证明必须
绑定**已签名发布清单的规范化 SHA-256**与相同 environment ID，
包含 1–32 个无凭据、无 query/fragment 的持久证据 URI 及内容哈希，并在 31 天
内失效。`pass` 中任何关键断言为假、计数不足或存在高危/严重发现都会被拒绝，
而不是降级为警告。

`signing-payload` 输出现有 Dyro 用途域隔离契约的精确 raw bytes、Base64 与
SHA-256；外部 signer/HSM 直接以 Ed25519 签署 raw bytes。`signature-attach`
只接受规范 Base64，使用 trust store 公钥验签成功后才 create-only 地生成完整
记录。两个命令都不接受 private/signing key 参数。跨语言签名系统可使用公开的
`dyro.signing.signature_message` 复现相同 RFC 8785 JCS 消息。

操作员使用同一条只读门禁命令完成验证：

```bash
dyro --root /control/dyro-profile runtime production-gate \
  --release-manifest /release/dyro-production-manifest.json \
  --security-attestation /evidence/prod-01.json \
  --provider-attestation /evidence/prod-02.json \
  --quota-attestation /evidence/prod-09.json \
  --human
```

缺少任一证明时只关闭已验证的对应项，其余项继续 `NOT_READY`/退出码 3。
文档篡改、签名用途错误、密钥撤销、过期、跨发布/环境漂移、参数角色错配或同一
公钥跨角色复用都返回验证错误/退出码 2。只有三份 `pass` 证明同时有效时才返回
`READY`/退出码 0；该结果仍明确要求独立发布批准，不会触发部署、导入、review、
signoff、merge 或 push。

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
| `runtime production-acceptance schemas` | 0 | — | 可选：仅创建新 schema 目录 |
| `runtime production-acceptance release-prepare` | 0 | 2 | 仅创建新未签名清单 |
| `runtime production-acceptance attestation-prepare` | 0 | 2 | 仅创建新未签名证明 |
| `runtime production-acceptance signing-payload` | 0 | 2 | 可选：仅创建新 raw payload |
| `runtime production-acceptance signature-attach` | 0 | 2 | 仅创建新已签名记录 |
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
| 发布绑定的四方签名生产验收 | 已实现 | production acceptance 契约、CLI 与对抗测试 |
| create-only operator/HSM 交接 | 已实现 | 真实文件稳定哈希、raw payload、外部签名附加与安装制品 smoke |
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
4. 发布清单与 `PROD-01/02/09` 证明通过当前时间、用途隔离、不同公钥、版本、
   环境和内容哈希绑定校验；
5. 独立 reviewer 确认 runtime 权限仍不包含交付控制动作；
6. canary、回滚、告警、on-call 与审计保留方案均完成演练。
