# Dyro 签名生产验收闭环审查

Date: 2026-07-30

Branch: `task/production-readiness-ux`

## 裁决

生产候选原有的“永久 `NOT_READY`”产品死路已关闭：真实环境完成
`PROD-01`、`PROD-02`、`PROD-09` 后，现在有一条发布绑定、用途隔离、可审计
且机器可验证的晋级路径。

**当前实际状态仍是 `NOT_READY`。** 本仓库没有真实生产环境的四方签名证据，
本轮实现也没有伪造这些证据。默认无输入门禁继续返回退出码 3。

## 用户视角走查

| 场景 | 旧体验 | 当前体验 | 结论 |
| --- | --- | --- | --- |
| 尚未完成真实环境验收 | 显示三个阻断项 | 行为不变，明确缺失检查、契约与签名用途 | 安全且可理解 |
| 已完成一项验收 | 系统无法接收，仍只能改代码 | 只关闭对应检查，其余阻断项和退出码 3 保留 | 可渐进追踪 |
| 已完成全部验收 | 没有可信输入通道，门禁永久关闭 | 一条命令验证同一发布的四方签名，成功返回 `READY`/0 | 闭环完整 |
| 证据写错或放错参数 | 可能只能靠人工辨认 | check ID、CLI 角色、签名 purpose、环境和清单 hash 必须一致 | 错误可定位 |
| 证据被篡改/过期/密钥撤销 | 无契约 | 返回验证错误/2，不把无效证据降级为警告 | fail-closed |
| 一个人用同一密钥自批四个角色 | 无约束 | 比较规范化公钥指纹并拒绝跨角色复用 | 降低自批风险 |
| 门禁通过后 | 容易误解为自动上线 | 输出 `release_approval_required=true`，CLI 提醒仍需独立批准 | 权限边界清楚 |

## 安全与契约结论

- 唯一放行输入是当前时间下有效的签名发布清单与验收证明；调用方不能再注入
  自定义全通过 checklist，也不能回拨验证时间。
- 发布清单固定版本、源码、镜像、制品、provider 和运维计划哈希。
- 三份证明固定发布清单规范化哈希、环境、有效期、强断言、证据 URI 与内容哈希。
- JSON 读取限 256 KiB，拒绝最终符号链接、非普通文件、重复字段、非有限数值、
  并发长度漂移和未知字段。
- `pass` 证明必须满足全部强断言、至少一次真实 canary/一个可写挂载且开放
  高危/严重发现为 0；签名 `fail` 证明仍保持阻断。
- Runtime 只验证并报告，不创建证明、不管理私钥、不部署，也不执行 Core
  import、review、signoff、merge 或 push。

## 仍需真实环境完成

| ID | 外部行动 |
| --- | --- |
| `PROD-01` | 在实际编排器上完成多宿主逃逸、租户边界、内核、存储和网络策略评审，关闭严重发现，由安全职责签名 |
| `PROD-02` | 对真实 Codex/Claude 舰队验证 provider digest、Broker-only 凭据、轮换、撤销、恢复和 canary，由 operator 职责签名 |
| `PROD-09` | 对全部生产可写挂载强制字节、inode、文件数配额并执行耗尽/并发租户测试，由资源隔离职责签名 |

此外，部署控制面必须保护 trust root、分离四个私钥职责、保存证据对象、演练
rollback/observability/on-call，并在门禁 `READY` 后执行独立发布批准。

## 验证证据

- Ruff：`src tests experiments scripts tools` 通过。
- 完整非 Docker 回归：`346 tests` 通过，`19` 个 Docker 测试按环境隔离跳过。
- Stage0–5 真实 Docker 回归：同一源码快照 `19 tests` 全部通过；无残留实验
  容器。
- 新生产验收/CLI/签名聚焦回归：Python 3.11 与 3.14 各 `30 tests` 通过。
- wheel 与 sdist：均可脱离源码安装；Stage1–5 bundle、handoff 入口和三个
  packaged schema 验证通过；默认门禁保持 `NOT_READY`/退出码 3。
- `twine check --strict`：wheel 与 sdist 均通过。
- `uv lock --check`、`compileall`、`git diff --check` 均通过。

## 发布结论

**Go for source/package review；No-Go for production traffic until real signed
acceptance exists。**
