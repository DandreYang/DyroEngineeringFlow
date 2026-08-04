# DyroEngineeringFlow

[English](README.md) | [简体中文](README.zh-CN.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Português (Brasil)](README.pt-BR.md) | [Русский](README.ru.md)

**DyroEngineeringFlow · `dyro` CLI** 是面向多仓团队的本地优先工程自动化与交付控制平台。它将开发线、Git worktree、Agent 启动、任务门禁、独立复核和合并审计统一到可版本化的工作区配置中。

**让工程从任务到交付持续流转。**

DyroEngineeringFlow 不绑定 Codex、Claude 或任何业务领域。每个团队通过 `dyro.toml` Profile 定义仓库、目录布局、Agent adapter 与交付策略；业务规则、模型成本和发布实践始终留在各自的 Profile 中。

## 本地 Web 控制台

可在任意目录运行 `dyro console`，打开已登记工作区的只读本地总览。它只绑定随机端口的 `127.0.0.1`，通过一次性 fragment secret 与浏览器建立会话，不暴露网络监听，也不提供浏览器写操作。

```bash
dyro console
dyro --workspace platform console --no-open
dyro --root /path/to/profile console
```

控制台展示工作区健康、需要关注的事项、Task 执行计数、活跃 Objective 和一条安全的下一步 CLI 命令。它不会启动 Agent、改变任务状态、标记最近使用、合并、push 或访问外部服务。只读边界与恢复方式见 [Console 设计](docs/designs/local-web-console.md)。

## 核心约束

- 一个任务只能属于一条开发线，不能混用功能版本与 Hotfix 工作区。
- 每个任务在独立 `git worktree` 的 `task/<id>` 分支中执行。
- 门禁由编排器实际执行，不能只采信 Agent 的口头回执。
- 复核同时绑定执行回执和各仓精确任务 HEAD；源码漂移会使复核失效。
- 任务通过独立复核后才能变为 `done`；默认必须显式确认才能合并或推送。
- 已完成的依赖只有在其精确任务 HEAD 合入所属开发线后，才会释放下游工作。
- 可执行配置使用 argv 数组，核心不会执行来自 TOML 的 shell 字符串。

## 架构与流程图

以下图示在 GitHub 上以 **Mermaid 直接渲染**（不是图片链接）。控制面把**团队 Profile**（仓库、adapter、gates、策略）与 **Dyro Core**（workspace、launch、dispatch、verify、merge）分开；运行态在 `.dyro/`。图例为中文；命令名、配置键与状态名与 CLI 一致。

### 分层架构

```mermaid
flowchart TB
  subgraph Profile["项目 Profile（团队提供）"]
    P1["repositories / layout / 基线"]
    P2["Agent adapter argv"]
    P3["gates / 回执模板 / 策略"]
  end

  subgraph Core["Dyro Core · dyro CLI（机制）"]
    W["workspace<br/>anchors · lines · doctor"]
    L["launch<br/>安全 argv 模板"]
    D["dispatch<br/>DAG · claim · 状态机"]
    V["verify<br/>gates · ledger"]
    M["merge<br/>预检 · 恢复 · push 策略"]
  end

  subgraph Runtime["工作区运行态 .dyro/"]
    R1["tasks / lines / changes"]
    R2["evidence · review · ledger"]
  end

  Profile --> Core
  Core --> Runtime
  Human["工程师 / 发布负责人"] --> Core
  Agent["本机 Agent CLI"] --> L
  Runner["隔离 Runner（可选）"] -.->|"evidence ZIP"| D
```

### 多仓工作区目录

```mermaid
flowchart TB
  WS["workspace 根<br/>dyro.toml"]
  WS --> REPO["repositories/"]
  WS --> DYRO[".dyro/"]
  WS --> VER["versions/ 或 layout.lines"]
  WS --> WT["worktrees/ 或 layout.tasks"]

  REPO --> API["services/api · Git anchor"]
  REPO --> WEB["services/web · Git anchor"]

  DYRO --> LINES["lines/&lt;id&gt;.toml"]
  DYRO --> TASKS["tasks/&lt;id&gt;/"]
  DYRO --> CHG["changes/ · decisions · ledger"]

  TASKS --> TT["task.toml · handoff.md"]
  TASKS --> EV["evidence-imports/ · review.md"]

  WT --> TAPI["task/API-101/services/api"]
  WT --> TWEB["task/API-101/services/web"]

  VER --> LAPI["release-…/services/api worktree"]
  VER --> LWEB["release-…/services/web worktree"]
```

### 任务状态机

```mermaid
stateDiagram-v2
  [*] --> backlog
  backlog --> assigned: claim / next 领取
  assigned --> in_progress: run 开始
  in_progress --> waiting_answer: 需要人工答案
  waiting_answer --> in_progress: task answer
  in_progress --> review: gates 通过 · 私有证据流程
  in_progress --> failed: 失败
  failed --> assigned: 重试领取
  review --> review_pending_signoff: 已验证复核 · 私有流程
  review --> done: 独立复核 PASS · 私有流程
  review_pending_signoff --> done: task signoff · 私有流程
  done --> [*]: task merge 进开发线
```

### 本地交付时序

```mermaid
sequenceDiagram
  actor Eng as 工程师
  participant CLI as dyro CLI
  participant FS as 工作区 Git / .dyro
  participant Agent as Agent adapter

  Eng->>CLI: setup / doctor / line create
  CLI->>FS: 登记 line · 创建 line worktrees
  Eng->>CLI: task create · task next
  CLI->>FS: 写 task.toml · 分配 worktree
  Eng->>CLI: task run / open --agent
  CLI->>Agent: argv launch 到 task worktree
  Agent-->>CLI: 工作结束（非 gate 证据）
  CLI->>CLI: 执行 Profile gates
  CLI->>FS: receipt · heads · attempt
  Eng->>CLI: task review
  CLI->>FS: 绑定 receipt 的 review
  Eng->>CLI: task merge --yes
  CLI->>FS: 合入开发线 · 更新 ledger
```

### 外部证据时序

```mermaid
sequenceDiagram
  actor Ctrl as 控制面操作者
  participant CLI as dyro 控制面
  participant Run as 隔离 Runner
  participant Rev as 独立复核方

  Ctrl->>CLI: task claim --by runner-id
  CLI-->>Run: claim 生效 · 冲突组占用
  Run->>Run: 在隔离区执行 · 跑声明 gates
  Run->>CLI: evidence build → ZIP
  Ctrl->>CLI: evidence execution --bundle
  CLI->>CLI: 校验 ZIP · heads · gates · 签名策略
  Rev->>CLI: evidence review / review-build
  CLI->>CLI: 绑定 receipt + task-heads
  opt require_external_signoff
    Ctrl->>CLI: task signoff --by approver
  end
  Ctrl->>CLI: task merge --yes
```

### 任务图

```mermaid
flowchart LR
  subgraph Nodes
    T1["Task A"]
    T2["Task B"]
    T3["Task C"]
    D1["Decision<br/>blocked_on"]
  end

  T1 -->|depends_on| T2
  T2 -->|depends_on| T3
  T3 --> D1

  CG["conflict_group: db-migrate<br/>同一 wave 互斥"]
  T1 -.-> CG
  T2 -.-> CG
```

### 调度波次

```mermaid
flowchart TB
  Snap["不可变调度快照<br/>graph + 状态 + claims"]
  Ready["ready 集合<br/>依赖已集成 · 决策满足"]
  Wave["本轮 wave<br/>--parallel × conflict_group"]
  Snap --> Ready --> Wave
```

### 用例总览

```mermaid
flowchart LR
  subgraph Actors
    Dev["开发工程师"]
    Lead["版本 / 发布负责人"]
    Runner["隔离 Runner"]
    Reviewer["独立复核人"]
  end

  subgraph UC["主要用例"]
    U1["初始化工作区 setup/init"]
    U2["创建开发线 / Hotfix"]
    U3["创建与调度任务"]
    U4["本地执行与 gates"]
    U5["外部证据导入"]
    U6["独立复核与签收"]
    U7["合并与 Change Set 校验"]
    U8["审计同步 Witness"]
  end

  Dev --> U1
  Dev --> U3
  Dev --> U4
  Lead --> U2
  Lead --> U7
  Lead --> U8
  Runner --> U5
  Reviewer --> U6
  Dev --> U6
```

### 多智能体分层（可选实验）

```mermaid
flowchart TB
  Host["宿主 Agent<br/>当前对话"]
  Disp["local_agent_dispatch<br/>契约 · 守卫 · 租约"]
  B1["后端 CLI A"]
  B2["后端 CLI B"]
  Board["对抗评审板<br/>签名区 + 终裁"]
  Dyro["Dyro 控制面<br/>claim · gates · merge"]

  Host -->|"TaskContract JSON"| Disp
  Disp --> B1
  Disp --> B2
  B1 -->|"summary + evidence"| Host
  B2 -->|"summary + evidence"| Host
  Host --> Board
  Board -.->|"仅建议"| Host
  Host -->|"显式 dyro 命令"| Dyro
```

随 `dyro` 安装分发（`dyro dispatch …`）。Dispatch 结果仅为建议；交付仍走 Dyro gates/merge。见 `docs/agent-orchestration-discipline.md`。

### 多智能体时序（可选实验）

```mermaid
sequenceDiagram
  participant H as 宿主 Agent
  participant S as DispatchSupervisor
  participant W as Worker 或后端
  participant P as 评审板文件

  H->>S: run --wait TaskContract
  S->>S: 校验 files · secret guard · 占槽
  S->>W: 自包含 prompt + 白名单上下文
  W-->>S: ResultEnvelope
  S->>S: locator verified 标记
  S-->>H: run_id · summary · evidence
  H->>P: 写入本人签名区
  Note over H,P: 不得改他人区 - 源码为准
  H->>H: 终裁后可选改代码 / 提 PR
  Note over H: 合入交付仍走 dyro task merge
```

## 快速开始

日常使用 CLI 时，推荐通过隔离的 `pipx` 环境从 PyPI 安装 `dyro`（要求 Python 3.11+）：

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# 执行 ensurepath 后请重新打开终端，再运行：
pipx install dyro
dyro --version
```

升级时运行 `pipx upgrade dyro`。若团队统一使用 `pip` 管理 Python 包，可改用：

```bash
python3 -m pip install --user --upgrade dyro
```

交互运行 `dyro`、`dyro home` 或 `dyro start` 时，Dyro 每个本地自然日最多访问一次官方 PyPI；断网、超时或状态目录不可写都不会阻塞进入工作区。默认仍由用户确认更新：

```bash
dyro update check
dyro update now
dyro update auto on      # 主动开启补丁版本自动更新
dyro update auto off
dyro update disable      # 同时关闭自动更新
dyro update enable
```

Dyro 会识别 `uv tool`、`pipx`、当前 `pip`，也能在没有 pip 的普通虚拟环境中回退到 `uv pip`，并在手动更新前展示无 shell 的完整命令。自动更新不会跨次版本或主版本，也不会覆盖 editable 源码安装；设置 `DYRO_NO_UPDATE_CHECK=1` 可临时跳过启动检测，而不修改已保存的偏好。详见[安全更新](docs/updates.md)。

若要开发 Dyro 本身，请使用仓库锁定的工具链和实际测试入口（不要把下文的“受控项目门禁”示例当成 Dyro 的测试命令）：

```bash
uv sync --locked --all-extras --dev
uv run python -m unittest discover -s tests -t . -v
uv run ruff check src tests experiments
```

项目提交的 Ruff 基线刻意只选择 `E4`、`E7`、`E9` 和 `F`。临时使用 `--select E,W,F` 属于更宽泛的风格审计，并不是 CI 配置的检查契约。

首次使用只需进入包含仓库的目录，或直接进入一个现有 Git 项目后运行：

```bash
dyro setup
```

首次引导会先展示计划，确认前不写入任何文件。它会扫描当前目录下的 Git 仓库、推断工作区相对路径与开发线挂载位置；若你在一个 Git 项目根目录运行，它会建议在同级创建独立 Dyro 工作区并从 `origin` clone，绝不移动、覆盖或把 Dyro 控制状态写入原项目。空目录时可直接输入一个 Git remote。

如果团队已经发布了工作区蓝图，新成员不需要重新理解仓库目录、分支或特殊基线规则，可以直接验证、预览并加入：

```bash
dyro blueprint validate git@github.com:acme/platform-blueprints.git --ref main
dyro join git@github.com:acme/platform-blueprints.git --ref main --dry-run
dyro join git@github.com:acme/platform-blueprints.git --ref main
```

`join` 默认创建到 `~/DyroProjects/<suggested_directory>`，在交互终端中让用户选择开发线，并只要求一次最终确认。蓝图必须为每个仓库声明完整、不可移动的提交 SHA；anchor 保持 detached，开发线使用隔离的 linked worktree。团队自己的仓库地址和规则只存在于团队蓝图，不进入 Dyro Core。完整格式见[工作区蓝图契约](docs/workspace-blueprints.md)。

引导会在最终确认前说明是否将创建 Profile、clone 缺失仓库、创建首条 `dev` 开发线或登记已检测到的受支持 Agent。它会探测常见的本机 Agent 命令，但只登记 Core 已审计 argv 契约的 adapter；发现但尚未集成的命令保持不动。输入 `n` 或直接退出不会留下半成品配置。完成后运行：

```bash
dyro next
```

它会给出当前唯一安全的下一步，例如创建开发线、配置 Agent，或启动已就绪的开发线。若你在脚本或 CI 中使用，保留显式参数和确认：

```bash
dyro setup . --name my-workspace --line dev --yes --non-interactive
```

安全预览既支持全局参数，也支持更符合直觉的命令后参数：

```bash
dyro --dry-run setup . --name my-workspace --no-line
dyro setup . --name my-workspace --no-line --dry-run
```

后续新增仓库也无需打开 `dyro.toml`：

```bash
dyro repo add repositories/services/payments
dyro repo list
```

常用交付策略和 Agent adapter 也无需打开 `dyro.toml`：

```bash
dyro config set policy.execution_mode external
dyro config get policy.execution_mode
dyro agent add ci-runner --preset noop
dyro agent test ci-runner
```

Profile 已配置 remote 时，可以安全补齐缺失仓库 anchor：

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

配置完成后，直接运行 `dyro`。第一次在项目内运行时，它会把该项目登记到可逆的全局首页；以后在任意目录运行同一个命令，都可以继续最近的开发线、Hotfix 或已有任务 worktree。新人不需要记住工作区路径或 `--root`：

```bash
dyro
```

交互式首页在启动前始终询问使用哪个编码工具，即使 Profile 只配置了一个
adapter 也不会静默进入。已配置 adapter 仍可参与 Dyro 的执行契约；仅在本机
检测到的受支持命令会标记为“仅打开工作区”，不获得门禁、复核、合并或 push
权限。`dyro open dev --agent codex` 等显式命令继续直接启动，便于脚本使用。

选择器会分别识别 Cursor Desktop 和 Cursor CLI，并把 OpenClaw 作为“仅打开
工作区”的外部运行时。可直接使用的工具始终排在待初始化或未安装工具之前；
当前工作区上次选择、项目推荐、个人默认和置顶顺序用于稳定排序。选择未安装但
受支持的工具后，会进入先展示计划、再明确确认的安装引导：

```bash
dyro tool list
dyro tool default cursor-desktop
dyro tool pin cursor-desktop codex openclaw
dyro --dry-run tool install openclaw
dyro tool install openclaw
```

安装配方是 Dyro 内置的无 shell argv，项目文件不能提供安装命令。对于需要执行
远程脚本的官方安装方式，Dyro 不会代为执行，只会在确认后打开官方页面。详见
[编码工具目录与安装引导](docs/tool-catalog.md)。
OpenClaw 工作区是默认工作目录，并不是操作系统级沙箱；初始化前会明确提示这一边界。

也可以显式登记、切换和查看所有项目；这些命令只管理全局入口，不会移动或删除项目：

```bash
dyro workspace add /path/to/workspace --name my-project --default
dyro workspace list
dyro --workspace my-project open dev --agent codex
dyro --workspace my-project task open API-101 --agent codex
dyro status --all
dyro agent discover
```

`task open` 只进入已经存在且通过 anchor、分支拓扑核验的任务 worktree，不执行任务、不改变状态。`agent discover` 会区分“已配置可启动”与“本机检测到但尚未集成”，不会绕过 Profile 授权。需要传统的开发线与 Agent 选择入口时，`dyro start` 仍保持兼容。

## 交付流程

版本负责人或自动化脚本可使用显式命令：

```bash
dyro doctor
dyro line create release-2026-10 --base origin/main --yes
# 仅在某个仓库需要时覆盖它自己的已核实基线。
dyro line create release-2026-10 --base origin/main --repo-base web=v2026.10.0 --yes
dyro open release-2026-10 --agent codex
dyro task create API-101 --title "Implement API contract" --line release-2026-10 --repository api
dyro task graph check --line release-2026-10
dyro task graph --line release-2026-10 --format mermaid
dyro task explain API-101
dyro task next
dyro task next --run --yes
dyro task attempts API-101
dyro task binding API-101
dyro task review API-101
dyro task merge API-101 --yes
dyro changeset create release-2026-10-ready --line release-2026-10
dyro changeset verify release-2026-10-ready
```

线上 Hotfix 必须明确已核实的生产基线，不能隐式继承默认分支：

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

如果执行与审批由独立受信任系统承担，可在 Profile 设置 `policy.execution_mode = "external"` 和 `policy.require_external_signoff = true`。此时本机 Dyro 仅允许计划核验；复核结论同时绑定回执和精确任务 HEAD 后，仍必须显式签收才能进入 `done`：

```bash
dyro task claim API-101 --by isolated-runner-1 --key-id runner-2026 \
  --output /secure-transfer/API-101.core-claim.json
# 长任务须在租约到期前续期 claim。
dyro task claim-renew API-101 --by isolated-runner-1 --lease-seconds 3600
# 在隔离 runner 中运行声明的门禁，并打包回执、日志和精确 HEAD。
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md --output /runner/out/API-101.zip
# 在控制面导入并校验这一份可移植证据包。
dyro task evidence execution API-101 --bundle /runner/out/API-101.zip
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
# 可显式释放已放弃的领取。
dyro task claim-release API-101 --by isolated-runner-1
```

新证据包必须包含 `provenance.json`。导入无 provenance 的遗留包属于刻意迁移，需要 `dyro task evidence execution API-101 --bundle ... --allow-legacy`。若外部 runner 返回 `QUESTION`，用 `dyro task answer API-101 --text "..."` 记录答案；现有 claim 保留，任务回到 `assigned` 以提交下一次证据。

查看并安全保留不可变证据世代：

```bash
dyro task evidence generations API-101
dyro --dry-run task evidence generations API-101 --prune --older-than-days 30 --keep 10
dyro task evidence generations API-101 --prune --older-than-days 30 --keep 10 --yes
```

加密身份方面：在工作区外生成密钥，只把公钥装入按用途分离的信任库：

```bash
dyro config set policy.execution_mode external
dyro config set policy.require_signed_execution true
dyro config set policy.require_signed_review true
dyro config set policy.require_external_signoff true
dyro config set policy.require_signed_signoff true

dyro key generate runner-2026 --private-key /secure/runner.pem --public-key /secure/runner.pub.pem
dyro key trust runner-2026 --purpose execution --public-key /secure/runner.pub.pem   --not-after 2027-01-01T00:00:00+00:00
dyro task claim API-101 --by isolated-runner-1 --key-id runner-2026 \
  --output /secure-transfer/API-101.core-claim.json
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md   --output /runner/out/API-101.zip --claim /runner/in/claim.json   --signing-key /secure/runner.pem --key-id runner-2026

dyro key generate reviewer-2026 --private-key /secure/reviewer.pem --public-key /secure/reviewer.pub.pem
dyro key trust reviewer-2026 --purpose review --public-key /secure/reviewer.pub.pem
dyro task evidence review-build API-101 --file /review/out/review.md --reviewer independent-reviewer   --output /review/out/review.json --signing-key /secure/reviewer.pem --key-id reviewer-2026
dyro task evidence review API-101 --file /review/out/review.json

dyro key generate approver-2026 --private-key /secure/approver.pem --public-key /secure/approver.pub.pem
dyro key trust approver-2026 --purpose signoff --public-key /secure/approver.pub.pem
dyro task signoff API-101 --by release-manager --signing-key /secure/approver.pem --key-id approver-2026

dyro key list --purpose execution --show-status
dyro key revoke runner-2026 --purpose execution --reason "runner retired"
dyro key audit
```

将本地信任审计链同步到独立 Witness：

```bash
dyro key generate audit-client-2026   --private-key /secure/audit-client.pem   --public-key /secure/audit-client.pub.pem
# 通过带外安全通道把 audit-client.pub.pem 安装到 Witness。
dyro key trust witness-2026   --purpose audit-receipt   --public-key witness-2026.pub.pem
dyro key trust witness-recovery   --purpose audit-recovery   --public-key witness-recovery.pub.pem

DYRO_AUDIT_TOKEN=... dyro key audit-sync   --witness primary   --endpoint https://audit.example.com/v1/dyro/batches   --signing-key /secure/audit-client.pem   --key-id audit-client-2026   --witness-key-id witness-2026   --witness-recovery-key-id witness-recovery
```

即使没有新事件，命令仍会发送已签名检查点；若响应丢失，会按原样重放已持久化的 pending batch。Witness 必须独立重算事件链、拒绝序号或链头分叉、签发可验证回执，并将 batch 与回执写入带保留锁的不可变存储。协议、密钥轮换与部署边界见 [Audit Witness 协议](docs/audit-witness-protocol.md)。

项目提供可部署的标准库 Witness 服务：`dyro witness serve`。默认要求 bearer token 与 TLS，仅在创建 `records/<batch-sha256>.json` 后推进检查点；崩溃恢复会还原未完成记录。生产环境应将可变检查点与不可变 `records` 归档分离：WORM/Object Lock 仅用于 records 归档，检查点使用可持久可变存储。密钥轮换、容器与 S3 Object Lock 见 [Witness 部署指南](docs/witness-deployment.md)。

签名强制由 `policy.require_signed_execution`、`policy.require_signed_review`、`policy.require_signed_signoff` 显式控制；删光所有受信任密钥也不会关闭已开启的策略。已签名执行 claim 绑定 `claim_id`、世代、runner 与执行密钥 ID。签名消息与执行计划哈希使用 RFC 8785 JSON Canonicalization Scheme 字节，非 Python runner 可复现同一载荷。独立复核方用 `dyro task evidence review-build` 产出已签名 JSON 信封。轮换无中断：先信任新 key ID，再切换签名方，重叠窗口保留旧密钥，最后经工作区受控密钥流程吊销。

最小 TypeScript 参考签名器与 Python/Node 互操作向量位于 `examples/typescript-runner/`，演示控制面期望的规范字节、签名域、Ed25519 调用与签名信封。

所有有写入风险的操作都支持先查看计划：

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## 命令地图

| 命令 | 作用 |
| --- | --- |
| 无子命令的 `dyro` / `home` / `workspace add/list/default/remove` | 从任意目录进入最近工作，或管理可逆的全局项目入口。 |
| `blueprint validate` / `join` | 验证团队自有的通用蓝图，并创建可续跑的隔离多仓工作区。 |
| `setup` / `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | 无需手改 TOML 地完成新人引导、仓库管理与开发线、Agent 选择。 |
| `doctor` / `status` / `status --all` | 验证并显示当前或全部已登记工作区状态。 |
| `line create/list` | 创建、登记和查看功能开发线。 |
| `hotfix create` | 从显式生产基线创建 Hotfix 开发线。 |
| `changeset create/list/verify` | 固化并核验一次多仓交付所包含的干净、精确 Git 提交组合。 |
| `config get/set` / `agent list/add/test/discover` / `tool list/install/default/pin` / `open` | 安全管理策略、adapter、工具发现与个人启动偏好，或在正确开发线启动 Agent。 |
| `task create/open/list/board/status/next/graph/explain/attempts/binding` | 创建或进入任务、管理状态，编译/校验任务图，解释调度，查看 provenance，输出精确复核绑定。 |
| `task run/answer/gates/review/signoff` | 执行任务、回答追问、运行门禁、申请独立复核；需要时记录外部签收。 |
| `task claim --output` / `task evidence build/execution/review` | 一次性领取任务并以“仅创建”文件交给隔离执行器，构建/导入可移植执行证据包，并导入与回执绑定的复核证据。 |
| `task merge` | 将已复核的任务分支合入所属开发线。 |
| `task loop/daemon/stats/decisions` | 受控批处理、调度、台账报表和决策门禁。 |
| `dispatch` | 可选本地多 Agent 派发（L0–L4）；仅建议，不替代 gates/merge。 |

实现细节见[零摩擦全局首页 ADR](docs/adr/0003-zero-friction-global-home.md)、[架构与 Profile 契约](docs/architecture.md)、[工作区蓝图契约](docs/workspace-blueprints.md)、[既有控制面迁移指南](docs/migrating-existing-control-planes.md)，以及维护者用的 [PyPI 发布说明](docs/publishing.md)。

## 语言与文档

README 提供英语、简体中文、韩语、西班牙语、法语、德语、巴西葡萄牙语和俄语版本。所有译本共享同一组命令、配置键、目录名和安全规则。当前 CLI 提示与扩展技术文档仍主要为中文；README 多语言支持不代表运行时已支持切换语言。

## 当前边界

DyroEngineeringFlow 提供完整的本地工作流闭环，以及让高保障团队在本机保持“仅计划”模式的策略控制。它不创建远端仓库、不携带 SaaS 凭证，也不负责供给外部 runner；但保留可移植外部执行证据包的交接契约。可选的本地 Agent 派发以 `experiments.local_agent_dispatch` 随安装包分发，并通过 `dyro dispatch …` 使用；其结论仅供参考，绝不替代 gates、复核、signoff 或 merge。本地多仓 merge 会统一预检并在失败时恢复；不同 Git 远端无法提供原子跨仓 push，因此部分推送失败会写入台账等待恢复。自动 merge 需要任务清单与本地策略双重许可。本项目采用 [MIT License](LICENSE)，并已发布为 [PyPI `dyro`](https://pypi.org/project/dyro/) 包。

### 与 Graph Engineering 的关系（可选读）

行业里有时把「多节点 + 路由/并行 + 校验」的工作拓扑称作 **Graph Engineering**（相对单 agent loop）。Dyro 的交付拓扑与之实质相近（TaskGraph、状态机、gates、复核、merge，以及可选的 dispatch 子图），但产品身份仍是 **交付控制面**，不是 agent 编排框架，也不是 Knowledge Graph / GraphRAG。dispatch 仅为建议。详见[架构文档](docs/architecture.md#与-graph-engineering-的关系可选读)。
