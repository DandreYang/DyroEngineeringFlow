# Dyro 生产候选与用户体验审查

Date: 2026-07-30

Repo: DyroEngineeringFlow

Branch: `task/production-readiness-ux`

Base: `a72243b1cec936ba2c68c104415f20dfa1232def`

本报告取代
[`2026-07-30-devline-adversarial-review-board.md`](2026-07-30-devline-adversarial-review-board.md)
中有关外部 runtime 生产阻断项的旧快照；旧文档保留为历史审查记录。

## 裁决

当前源码、CLI 与安装制品达到 **Production Candidate**，可以进入正式代码
评审和真实环境验证；外部语义运行时的生产部署仍为 **NOT_READY**。

该结论是刻意分层的：

- 本地产品闭环、签名证据交接、安装包和 fail-closed 门禁已经具备；
- `PROD-01`、`PROD-02`、`PROD-09` 必须在真实发布环境取得证据；
- 在三个阻断项关闭前，不提供宽泛的 `runtime run`，也不允许 runtime 拥有
  evidence import、review、signoff、merge 或 push。

## 以 Dyro 用户身份走查

| 用户旅程 | 用户预期 | 实际体验 | 结论 |
| --- | --- | --- | --- |
| 第一次查看 runtime | 快速知道它能做什么、是否可上线 | `runtime status` 明确显示 Production Candidate、3 个阻断项、控制边界和下一命令 | 通过 |
| 排查本机 | 不改状态地知道 Docker/runtime/provider 是否可用 | `runtime doctor` 只读检查 lock、CLI、daemon、钉扎镜像和可选 provider；明确“本机可用不等于生产就绪” | 通过 |
| 制定上线计划 | 不必从多份文档拼装顺序 | `runtime plan` 按本机、Core handoff、真实环境、最终决策分阶段，并给出验收标准和检查命令 | 通过 |
| 向 runner 交付任务权力 | 目标已存在时不覆盖文件、也不先领取任务 | `task claim --output` 在领取前检查并原子保留目标，完整写入后才开放 `0600` 权限；拒绝符号链接和已有路径 | 通过 |
| Runner 准备 Stage5 claim | 权限只能缩减且 dry-run 无副作用 | 派生 claim 绑定 Core claim ID、代次、runner、key 与到期上限；dry-run 只验证 | 通过 |
| Stage5 交回执行证据 | 能先预检，失败时可诊断，成功时知道下一步 | handoff 重新验证 seal、artifact、claim、workspace 和密钥；失败返回 3 且仅保留诊断包，成功才显示 Core import 命令 | 通过 |
| 控制面完成交付 | Runner 不能自行宣布完成或发布 | Core 显式 import，独立 review/signoff 后才可 merge/push | 通过 |
| 从 wheel/sdist 安装 | 安装后仍有完整 runtime 资源和一致门禁 | 两种制品均在全新环境中完成 Stage1–5 bundle/manifest 组装，production gate 均返回 3 | 通过 |

### 用户体验改进

1. 人在终端中默认得到简短中文说明；管道消费默认得到稳定 JSON，也可显式
   使用 `--human` / `--json`。
2. `status → doctor → plan → claim → handoff → Core import` 形成可发现的命令
   路径，每一步都给出下一动作。
3. 退出码固定为：成功 `0`、输入/执行错误 `2`、生产或 gate 阻断 `3`。
4. 阻断项不再只有 ID；每项包含分类、现状证据、修复动作和验收证据。
5. dry-run 不执行 gates、不固定 HEAD、不签名、不创建 ZIP，也不声称可以导入。

## 本轮发现并关闭的问题

| 风险 | 修复 |
| --- | --- |
| `NOT_READY` 可能以成功退出码被 CI 忽略 | 门禁固定返回 3；CI/PyPI workflow 同时核对退出码、verdict 与结构化字段 |
| Stage5 pack 与 Core 证据链断开 | 新增受 Core claim 约束的签名 handoff，复用 Core gates、HEAD、provenance 与签名策略 |
| claim/evidence 输出可被覆盖或经符号链接逃逸 | create-only 原子发布、no-follow、限长读取、私密权限与并发覆盖回归 |
| dry-run 可能制造“已签名/已验证”错觉 | 结果显式区分 gates、HEAD、signature、bundle 是否真实执行或创建 |
| pack 后 artifact、claim 或 workflow 身份可漂移 | handoff 前重新核对 seal、跨文件 ID、canonical input、artifact 哈希和 Core binding |
| 导出的 claim 之后被释放、接管或密钥被撤销 | Core import 重新匹配当前 claim generation 与 trust 状态；Runner 的“可提交”不等于控制面批准 |
| 私钥可能放在不可信 workspace/pack 中 | 要求 `0600` 普通非符号链接文件，并强制位于 Profile、workspace、pack 之外 |
| Docker 删除后 daemon 延迟导致误判或误删同名新容器 | 按精确 ID 与 ownership label 删除，并在有界 settle 窗口持续证明名称和已观察 ID 均消失 |
| 源码可用但发布包缺 runtime 资源 | 安装后验证工具覆盖 doctor、handoff 入口和 Stage1–5 资源组装 |

## 验证证据

- Ruff：`src tests experiments scripts tools`，通过。
- `git diff --check`：通过。
- 聚焦回归：`63 tests`，通过；包含真实 Docker Stage0、cleanup settle、
  Core handoff、runtime CLI 与 doctor。
- 完整回归：`333 tests in 76.312s`，通过。
- wheel 与 sdist：均在独立 Python 3.13 环境安装并脱离 checkout 验证。
- `twine check --strict`：wheel 与 sdist 均通过。
- 安装后人工旅程：`runtime --help/status/doctor/plan/production-gate` 通过。
- 安装后门禁：wheel/sdist 均为 `NOT_READY`，退出码均为 `3`。
- wheel SHA-256：
  `541b5099ea0bc757f7c5786dc2fc888d21ebfa136928a0f640509df18fc9f393`
- sdist SHA-256：
  `fa51da68635cea4995dcc4299e5217ac4647a232c7fb2aead4a58dda11af421d`

## 真实上线阻断项

| ID | 必须补齐的生产证据 |
| --- | --- |
| `PROD-01` | 实际编排器、内核、存储和网络策略的独立多宿主逃逸/租户边界评审及修复闭环 |
| `PROD-02` | 真实 Codex/Claude provider 舰队 canary；二进制内容钉扎；凭据仅 Broker 可见；轮换、撤销、故障恢复演练 |
| `PROD-09` | 每个生产可写挂载的字节、inode、文件数强制配额，以及耗尽和并发租户故障测试 |

上线批准还应绑定具体环境、镜像 digest、provider digest、发布版本、值班人、
回滚步骤和 canary 结果。没有这些外部证据时，本地测试不得被解释为生产批准。

## 发布建议

**Conditional Go for source/package review；No-Go for production traffic。**

建议下一阶段以真实预生产环境依次完成 `PROD-01 → PROD-02 → PROD-09`，
保存可审计证据后重新运行 `dyro runtime production-gate`。只有门禁返回
`READY`/退出码 0，且独立复核确认权限边界未扩大，才进入 canary 与生产流量。

本审查没有执行 commit、push、merge、tag 或发布。
