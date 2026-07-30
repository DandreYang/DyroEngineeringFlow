# Dyro 开发线对抗评审板（外部语义运行时 + 本地 Agent 派发）

Date: 2026-07-30  
Repo: DyroEngineeringFlow  
权威复审目标：`task/adversarial-review-remediation` 未提交工作树（基线 `67bef22`，目标版本 `0.5.1`）
历史原评范围：`v0.4.1..67bef22`（原 `main` 结案快照，已被本次 remediation 复审取代）

> 复审更正：原 `Conditional Go` 在独立评审 B/C 完整回填前过早结案，现已撤销。后续发现与修复以 `task/adversarial-review-remediation`（基线 `67bef22`，目标版本 `0.5.1`）的 remediation 复审为准。

## Scope

- `experiments/external_workflow_runner/`（Stage0–5 外部语义运行时 PoC）
- `experiments/local_agent_dispatch/`（ADR-0002 L0–L4）
- `docs/adr/0001*`, `docs/adr/0002*`, `docs/agent-orchestration-discipline.md`, `docs/designs/optional-local-agent-dispatch.md`, `docs/external-semantic-runtime-poc.md`
- `tests/test_external_workflow_runner_stage*.py`, `tests/test_local_agent_dispatch*.py`
- 打包：`pyproject.toml` experiments 包、`dyro dispatch` / `dyro runtime`
- `.github/workflows/ci.yml` 中相关 job

## Reviewed Materials

- 上述路径源码与单测
- Stage5：`PRODUCTION_NOT_READY.md`、`POC_EVALUATION.md`、`production_gate.py`
- SSOT：ADR-0001、ADR-0002（含 2026-07-30 分发面修订）

## Rules

1. 源码/测试/现有契约 > 计划或自述。
2. 无法从源码证明 → `须人工核`。
3. Findings 用 P0/P1/P2；优先 bug/安全/边界破坏/测试空洞。
4. Agent / dispatch 输出不得被当作 Dyro gate。
5. 不因「风格偏好」开 P0。

## Fixed Decisions（结案后）

1. 外部语义运行时为 **可选模块**，可随 `dyro` wheel 安装；**不**替代 Core 交付控制；生产 **NOT_READY**。
2. Supervisor / dispatch 路径 **禁止** merge / push / signoff / 生产 evidence import。
3. 生产门禁保持 Stage5 **NOT_READY**（见 `PRODUCTION_NOT_READY.md`）。
4. 本地派发与 Docker 语义运行时 **并列不合并** 为一条产品线。
5. **分发修订（2026-07-30）**：`experiments.*` **进入** `dyro` wheel；相对 Core 仍为可选面（supersede 早期「不进安装包」表述）。

## Open Decisions（非阻塞）

1. 默认 CI 是否长期强制跑全量 Stage0–5 Docker isolation？（当前：独立 job 保留）
2. `examples/typescript-runner` 是否改为纯 Python 向量？（不阻塞）

---

# Reviewer Sections

原结案时多模型评审未全部回填；随后 Reviewer B（local dispatch）与 Reviewer C（packaging/CI/contract）均给出 **No-Go**，证明原仲裁不能继续作为放行依据。

### 源码核验摘要（主仲裁代填）

| 主题 | 结论 | 证据 |
| --- | --- | --- |
| Stage5 生产门禁 | NOT_READY 强制 | `production_gate.py` + 单测断言 verdict |
| Evidence | 仅 dry-run，无 Core import | `evidence_dry_run.py` / POC 表 |
| Supervisor 禁 merge/push | 配置与 dry-run 拒绝 production actions | stage5 supervisor + dry-run |
| Dispatch L0–L4 | 契约/租约/适配器/panel/bridge 有测 | `tests/test_local_agent_dispatch*.py` |
| 进包 | wheel 含 `experiments.*`；CLI `dyro dispatch` | `pyproject.toml`、`dyro.cli` |
| 边界 | 派发不调用 Core merge | 新增回归测试（结案批次） |

### 产品化残留风险（不计入本轮 source-review P0/P1）

| ID | 严重度 | 说明 |
| --- | --- | --- |
| R1 | PROD blocker | 真实 codex/claude 登录态依赖本机环境，CI 以 echo/fixture 为主（PROD-02） |
| R2 | PROD blocker | POC-24 worktree quota 仅 host 路径 partial（PROD-09） |
| R3 | P2 | 语义运行时尚无完整产品级 `dyro runtime run`（仅 status/NOT_READY 入口） |

---

# Original Arbitration（已撤销）

**日期**：2026-07-30  
**原裁决**：~~Conditional Go~~（因遗漏阻塞项，于同日复审撤销）

### 原 Go 条件（复审证明并未全部满足）

1. Core 与 experiments 分层清晰；dispatch/runtime **不**替代 gates/merge。
2. Stage5 生产 **NOT_READY** 有代码门禁与文档。
3. 单测覆盖 Stage0–5 与 local_agent_dispatch L0–L4 主路径。
4. 分发面文档与 ADR 已与「进 wheel」对齐。
5. 增加 wheel 安装冒烟与「dispatch 不触达 merge」回归。

### No-Go 项（明确不做）

- 将语义运行时标为 production ready。
- 将 dispatch 结果写入 TaskGraph 成功条件。
- 在未清 PROD-01..03/09 前开启 Core evidence import from Stage5 pack。

### 后续 backlog（不阻塞本结案）

- 真机 agent 舰队与密钥库（PROD-02）
- 多宿主逃逸评审（PROD-01）
- 可选：完整 `dyro runtime` 执行面（须新 ADR 清 NOT_READY）

**原签字**：主仲裁（维护者 / 结案批次，基于当时不完整证据）

---

# Remediation Re-Arbitration

**修复分支**：`task/adversarial-review-remediation`

**基线**：`67bef22`

**目标版本**：`0.5.1`（不覆盖已发布的 `0.5.0`）

### 阻塞项闭环

| 原发现 | 严重度 | 修复状态 | 核心证据 |
| --- | --- | --- | --- |
| wheel/sdist 缺 `ts_runtime` 与 Stage1–5 `bundle_src` | P0 | 已修复 | `pyproject.toml` package-data；安装后 Stage1–5 真组装并复验 manifest |
| strict/read-only/edit 能力边界与文档不实 | P0 | 已修复 | 真实 CLI strict fail-closed；Codex 固定 sandbox/network；edit detached worktree + hash patch |
| 默认异步 run 永久停在 accepted | P1 | 已修复 | detached worker + `worker` 子命令；默认异步回归 |
| run 重复执行、lease ABA、无原子所有权 | P1 | 已修复 | 文件锁、run CAS、worker/lease owner token、heartbeat |
| timeout 漏进程树、输出无界 | P1 | 已修复 | 新 session/process-group TERM→KILL、有界 selector 读取 |
| ContextGuard TOCTOU、尾部漏扫、宽 glob/输入无界及无 `dir_fd` 平台兼容 | P1 | 已修复 | POSIX no-follow 稳定 fd；无 `dir_fd` 时路径链前后快照 + 单 fd 身份复核；单文件/总量预算；Windows smoke 真读取 |
| GC 越界删除、非 JSON 结果仍 completed、空证据 100% | P1 | 已修复 | canonical containment；严格 JSON；空证据比例为 0 |
| panel 串行、backend 只看 PATH | P2 | 已修复 | 有界线程并行；安装+登录态+能力探测 |
| claim 续租覆盖新 owner、Supervisor 清理短路、broker 启动期间租约可过期 | P0/P1 | 已修复 | exact-record CAS；Stage2–5 在 broker startup 前启动续租；finalizer 错误聚合；claim no-follow/有界读取 |
| Docker readiness/partial-start 可能泄漏，daemon 故障被当成 absence | P0/P1 | 已修复 | 所有创建异常/非零返回均聚合清理；1 秒 daemon settle 复查；容器+网络显式 absence proof |
| Docker 同名冲突可能误删其他运行资源 | P1 | 已修复 | Stage1–5 统一 128-bit ownership label；删除前核验 label，仅按资源 ID 删除；不匹配保留并 fail-closed |
| artifact 与 envelope/manifest/ZIP 可分裂或路径逃逸 | P0/P1 | 已修复 | envelope digest 绑定；稳定文件读取；manifest seal；ZIP 成员/大小/路径一致性校验 |
| ZIP central directory 内存放大、换包 TOCTOU、伪造大小/多盘字段 | P1 | 已修复 | ZipFile 前有界 EOCD/central 解析；单个 no-follow fd 私有快照同时 hash；实际解压流计数；`disk_start == 0` |
| 非 POSIX backend probe/auto 认证语义分裂 | P1 | 已修复 | probe、auto、panel、supervisor 统一平台感知认证谓词；非 POSIX 仅 Echo 可选 |
| 顶层 `--dry-run`/`--root` 无法组合实验入口 | P1 | 已修复 | 先解析全局安全选项再路由；无状态 dry-run 回归 |
| wheel/sdist smoke 共用默认 dispatch HOME | P2 | 已修复 | CI 与 PyPI workflow 的 doctor/gate 各绑定 wheel、sdist 专属 HOME |
| lease heartbeat 失败或 lifecycle cleanup 失败覆盖 worker 主错误 | P1 | 已修复 | heartbeat failure 立即触发 exact backend cleanup；failed/timeout 保留主状态与主错误，cleanup 失败仅追加 lifecycle warning |
| cleanup 未证明时 slot 被 dead-owner/旧 schema 绕过回收 | P1 | 已修复 | lease 绑定 `run_id`；受信 `runs/` 反向扫描兼容旧 lease；active/unknown 引用永久 quarantine，仅 proven terminal/orphan 可回收 |
| lease scope symlink 与 partial acquire/中断回滚可逃逸或泄漏容量 | P1/P2 | 已修复 | root/scope/slot 全部 descriptor-relative + no-follow；第二阶段异常释放 backend；post-write `KeyboardInterrupt` 可证明回滚两个 slot |
| async accepted→poll 竞态绕过 reaper/cleanup | P1 | 已修复 | `poll()` 发现退出后同步进入 `_reap_async_worker`，在终态前完成 exact backend cleanup |
| ContextGuard 最终 open 被 FIFO 替换后可能阻塞 | P1 | 已修复 | 最终 fd 使用 `O_NONBLOCK | O_NOFOLLOW`，并在 regular→FIFO 竞态回归中证明快速拒绝 |

### 验证证据

- Ruff：`src tests experiments tools` 全绿。
- Compileall：`src tests experiments tools` 全绿。
- 非 Docker 全库（沙箱外真实 POSIX 进程代际）：`302 tests`，`OK (skipped=19)`；19 项均为本轮刻意屏蔽 Docker 的用例。
- Docker 可挂载 checksum 精确快照：Stage0–5 与对抗性外部运行器共 `99 tests`，`OK`，无跳过；快照前后无源码文件差异，仅保留既有 `3` 个 Dyro 容器与 `1` 个 Dyro 网络，无新增残留。
- 外部运行器针对性对抗套件：`26/26`；另有 Stage2–5 startup authority 表驱动回归纳入非 Docker 全库。
- local dispatch 对抗模块：`80 tests`、`OK (skipped=1)`；与 L1–L4 套件合跑为 `87 tests`、`OK (skipped=1)`。该项仅在受限沙箱因无法读取稳定进程代际而跳过，已在沙箱外全库实际执行通过。
- wheel 与 sdist（final12）：`/tmp/dyro-051-final12-dist.vfzlr0/dyro-0.5.1-py3-none-any.whl` 与 `/tmp/dyro-051-final12-dist.vfzlr0/dyro-0.5.1.tar.gz` 分别安装到全新 venv 并脱离 checkout 验证；版本均为 `0.5.1`，`dyro`、local dispatch、external runner 均从各自 `site-packages` 导入，Stage1–5 bundle/manifest 与独立 HOME 下的 `dispatch doctor` 全部通过。
- final12 SHA-256：wheel `338951cf018d416254082e245f2910a6d0796f7ee4603f1ea2329f06b1353e33`；sdist `0d1f19150e46ec0de9ee66d43067cc79e0d45d1aa41300bcf96fb04228fc9bba`。
- 两种 final12 产物在各自 doctor HOME 下执行 `runtime production-gate`，均保持 `NOT_READY`，阻断项仍为 `PROD-01/02/03/09`，未发生误放行。
- 独立 local dispatch 最终复审覆盖 run-bound/legacy quarantine、scope symlink、partial acquire 与 post-write interrupt rollback；P0/P1/P2 均为 0，结论 `GO`。
- 独立外部运行器复审先后发现 ZIP central 预分配、ZIP 换包/多盘、创建异常漏清理、同名资源误删等阻断项；全部修复后最终复审 P0/P1 均为 0，结论 `GO`。
- 最新独立复审提出的 PyPI HOME 隔离、Windows fallback smoke、Stage2–5 startup authority 与 local lease 异常安全问题均已闭环并复核通过。
- CodeRabbit CLI 多轮 findings 均已逐条核验：平台前置 guard 已补齐；粗粒度进程 token 相等本就返回“无法证明死亡”并 fail-closed，另补语义注释与回归。最后一次针对当前完整差异的实际扫描成功完成，结果为 `findings: 0`。
- 静态门禁：Ruff、Compileall、`uv lock --check`、GitHub Actions YAML 解析与 `git diff --check` 全部通过；Windows smoke 已加入真实 fallback 读取，但仍仅由 CI Windows job 执行，本机 macOS 未冒充 Windows 验证。

### 新裁决

**裁决**：**Conditional Go for 0.5.1 source review**（P0/P1 = 0）。

条件与边界：

1. 仅表示本修复集具备进入正式 code review / merge 决策的证据；当前尚未 commit、push、tag 或发布。
2. 已发布 `0.5.0` 的缺资源产物不可覆盖，必须以新的 `0.5.1` 发布流程交付。
3. 外部语义运行时生产门禁继续保持 **NOT_READY**；本轮不启用 Core evidence import、signoff、merge 或 push。
4. 真 Codex/Claude 的账户与模型行为仍依赖本机；严格隔离任务不得使用这两个 adapter，应走 ADR-0001 Docker 链。
5. 最终 CodeRabbit 已对当前完整差异完成扫描并返回 `0 findings`；Windows 实际 smoke 仍须由 CI Windows runner 补齐。

**复审签字**：主执行者（修复与本地验证）+ 独立复审（P0/P1=0，GO）；仍需维护者授权 commit / merge / release。
