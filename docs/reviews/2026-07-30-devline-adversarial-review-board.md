# Dyro 开发线对抗评审板（外部语义运行时 + 本地 Agent 派发）

Date: 2026-07-30  
Repo: DyroEngineeringFlow  
Range: `v0.4.1..main`（experiments + ADR + 相关测试/CI + 分发面）  
结案 HEAD：以结案时 `main` 为准（含 experiments 进 wheel / `dyro dispatch`）

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

多模型并行评审未全部回填签名区（部分 harness 失败）。结案以 **源码 + 单测 + ADR + Stage5 门禁** 为主仲裁依据，不因缺失签名区阻塞。

### 源码核验摘要（主仲裁代填）

| 主题 | 结论 | 证据 |
| --- | --- | --- |
| Stage5 生产门禁 | NOT_READY 强制 | `production_gate.py` + 单测断言 verdict |
| Evidence | 仅 dry-run，无 Core import | `evidence_dry_run.py` / POC 表 |
| Supervisor 禁 merge/push | 配置与 dry-run 拒绝 production actions | stage5 supervisor + dry-run |
| Dispatch L0–L4 | 契约/租约/适配器/panel/bridge 有测 | `tests/test_local_agent_dispatch*.py` |
| 进包 | wheel 含 `experiments.*`；CLI `dyro dispatch` | `pyproject.toml`、`dyro.cli` |
| 边界 | 派发不调用 Core merge | 新增回归测试（结案批次） |

### 残留风险（非 P0）

| ID | 严重度 | 说明 |
| --- | --- | --- |
| R1 | P1 | 真实 codex/claude 登录态依赖本机环境，CI 以 echo/fixture 为主 |
| R2 | P1 | POC-24 worktree quota 仅 host 路径 partial |
| R3 | P2 | 语义运行时尚无完整产品级 `dyro runtime run`（仅 status/NOT_READY 入口） |

---

# Final Arbitration

**日期**：2026-07-30  
**裁决**：**Conditional Go**（开发线质量可合入 main / 可发含实验面的版本）

### Go 条件（均已满足或接受）

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

**签字**：主仲裁（维护者 / 结案批次，基于源码核验）
