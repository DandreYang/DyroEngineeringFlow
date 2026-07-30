# ADR-0003：外部语义运行时生产晋级契约

- 状态：已接受（Production Candidate 基线；**不等于生产放行**）
- 日期：2026-07-30
- 决策者：DyroEngineeringFlow 维护者
- 关联：[ADR-0001](0001-optional-external-semantic-runtime.md) ·
  [生产就绪设计](../designs/external-runtime-production-readiness.md)

## 背景

Stage0–5 已证明固定 first-party TypeScript workflow 在本机三域隔离架构中
可运行，也能在 Sandbox 与 Broker 双重清理后封存 evidence pack。但原有产品面
只有 `status` 和一个始终返回退出码 0 的 `production-gate`：

- CI 会把 `NOT_READY` 误判为命令成功；
- operator 看不到本机缺少什么、下一步该做什么；
- Stage5 claim 与 Dyro Core claim 使用不同的租约代次语义；
- Stage5 pack 无法进入 Core 已有的 receipt、gates、HEAD、provenance 与
  independent review 绑定链。

生产晋级需要修复这些交接问题，同时保持 ADR-0001 的控制面边界。

## 决策

### 1. 生命周期与门禁语义

外部 runtime 使用三个明确状态：

1. `local experiment`：只证明本机隔离；
2. `production candidate`：产品契约和 Core 交接已实现，但真实环境证据未齐；
3. `production ready`：全部代码与环境阻断项均通过独立复核。

`dyro runtime production-gate` 在 `NOT_READY` 时返回退出码 **3**，在
`READY` 时返回 0；参数或运行错误使用退出码 2。只想查看状态时使用
`dyro runtime status`，它始终是只读查询。

### 2. Dyro Core 保持唯一交付控制面

Runtime 可以：

- 执行固定、已评审、内容哈希锁定的 workflow；
- 通过 Broker 调用 provider；
- 在双重清理后封存 runtime evidence；
- 构建一个供 Core 导入的签名 execution bundle。

Runtime 永远不能：

- 导入 execution evidence；
- 作出或导入 independent review；
- signoff、merge 或 push。

这些权限仍由 Dyro Core 与独立主体掌握。

### 3. Claim 权限只能缩减，不能扩张

`dyro task claim --output …` 导出当前 Core claim。Runner 使用
`dyro runtime claim prepare` 生成 Stage5 claim，必须保留：

- `control_claim_id`
- `control_generation`
- `runner`
- `execution_key_id`
- `authority_expires_at`

Stage5 内部续租可以递增自己的 lease generation，但到期时间不得超过
`authority_expires_at`。Core claim 已过期时拒绝准备、续租或 handoff。
导出的 Core claim 与派生 Stage5 claim 都采用 create-only `0600` 文件；
handoff 以 no-follow、限长读取方式拒绝符号链接、非普通文件和宽松权限。

### 4. Stage5 pack 通过 receipt 进入既有 Core 证据闭包

`dyro runtime handoff` 执行以下 fail-closed 步骤：

1. 验证 pack manifest、seal、ZIP、claim、workflow ID、artifact 与双重清理；
2. 验证 pack 的 control claim 与同一份 Core claim 快照一致；
3. 重新核对 runner workspace 中的 artifact 内容仍与 pack 哈希一致；
4. 生成包含 pack SHA-256、canonical input 与 control claim 绑定的
   `receipt.md`；
5. 调用 Core 已有 evidence builder 运行声明的 gates、固定干净逐仓 HEAD、
   构建 provenance，并使用 claim 绑定的 execution key 签名；
6. 只输出 ZIP，不调用 Core import。

控制面随后显式执行 `dyro task evidence execution`，再由独立 reviewer 绑定
相同 receipt、HEAD、attempt 与 plan。Import 时必须重新匹配控制面当前 claim
与 trust 状态；已释放/被新 generation 接管的 claim 或已撤销密钥会使旧
bundle 失效，Runner 的 `ready_for_core_import` 不构成控制面批准。

私钥必须是 `0600` 的普通非符号链接文件，并且物理路径必须位于 Dyro
Profile、runner workspace 与 Stage5 pack 之外。Handoff 在 dry-run 与真实
构建前都执行该路径预检，减少不可信工作区读取或替换私钥的机会。

### 5. 本机诊断不能替代生产环境验收

`dyro runtime doctor` 只读检查 runtime lock、Docker daemon、钉扎镜像与
可选 provider 内容钉扎。即使全部通过，也只代表本机 PoC 可运行。

下列证据必须来自实际发布环境：

- 多宿主/容器逃逸与租户边界评审；
- 真实 provider 舰队、凭据交付/轮换/撤销与故障恢复；
- 每个可写挂载的字节、inode 和文件数强制配额及耗尽测试。

不得用可编辑的 `pass: true` 配置、fixture 或本机结果伪造这些证据。

### 6. 真实环境证据以发布绑定的独立签名进入门禁

门禁不再永久硬编码环境阻断状态，也不接受调用方传入自定义 checklist。真实
环境完成验收后，唯一可清除 `PROD-01/02/09` 的路径是：

1. `production-release` 签名的发布清单，固定 Dyro 版本、源码 commit、镜像、
   wheel/sdist/SBOM/provenance、provider 二进制与运维计划内容哈希；
2. `production-security`、`production-provider`、`production-quota` 分别签署
   对应检查，并绑定同一份已签名清单哈希和 environment ID；
3. 发布与三个验收角色使用四把不同的 trusted Ed25519 公钥；
4. 证明有界、限期、包含持久证据 URI 与内容 SHA-256，且当前密钥未撤销；
5. 所有 `pass` 强断言都满足，开放高危与严重发现均为 0。

门禁只验证并汇总，不创建、修改或签署上述证据，也不触发部署。缺少证明时保持
原始阻断；签名的 `fail` 仍是阻断；无效或过期证明以验证错误失败关闭。即使全部
验证得到 `READY`，结果也包含 `release_approval_required=true`，最终发布批准
仍由独立发布控制面作出。

## 后果

- `PROD-03`（Stage5→Core execution evidence handoff）可由代码和端到端测试
  判定为通过。
- `PROD-01`、`PROD-02`、`PROD-09` 仍阻断生产；因此当前结论仍是
  `NOT_READY`。
- 三项真实环境验收完成后已有可审计、可机器验证的晋级路径；默认无外部证据时
  行为不变，仍为 `NOT_READY`/退出码 3。
- 外部 runtime 对 Core 的依赖方向仅存在于可信 handoff adapter；
  Core 不依赖 Bun、TypeScript runtime 或 Stage5 Supervisor。
- 生产 operator 执行面必须由部署适配层固定 Stage5 配置；在真实 provider
  与凭据契约完成前，不提供“任意 workflow”或自动生产运行命令。

## 否决项

- 把 `doctor PASS` 当成生产放行。
- 允许 Stage5 lease 超过 Core claim 到期时间。
- pack artifact 与 handoff workspace 内容不一致时继续构建 Core bundle。
- Runtime 自动执行 import、review、signoff、merge 或 push。
- 通过用户自填状态清除生产环境阻断项。
- 用同一公钥跨发布、安全、provider 或配额角色完成自我批准。
- 验收证明不绑定精确发布清单、环境或有效期。
