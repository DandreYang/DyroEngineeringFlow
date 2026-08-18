# 架构与 Profile 契约

新人图示导览（架构 / 目录 / 时序 / 用例 / 多智能体）：[`diagrams.md`](diagrams.md) · [English diagrams](diagrams.en.md)

## 分层

```text
DyroEngineeringFlow Core（`dyro` CLI）
  ├─ home: 全局工作区入口、最近目标、任意目录导航（无交付权限）
  ├─ workspace: anchors、逐仓基线、开发线、Hotfix、存储模式、doctor
  ├─ launch: Agent adapter 的安全 argv 模板
  ├─ dispatch: 任务 DAG、决策点、冲突组、状态机、回执、复核与外部签收
  ├─ verify: gates、日志、台账和统计
  └─ merge: 全仓预检、失败恢复、显式合并和受策略约束的 push

Project Profile
  ├─ repositories / layout / 基线策略
  ├─ Agent adapter 与本机环境
  ├─ 业务检查单、回执模板、门禁命令
  └─ 发布、合规和提交策略
```

Core 只提供机制，不嵌入仓库名称、客户信息、模型价格、内网地址、发布平台或业务规则。

`home` 位于 Profile 之上，只保存工作区别名、绝对路径和最近使用的目标/Agent，不保存凭据，也不拥有 gates、review、signoff、merge 或 push 权限。当前目录中的 Profile 优先于全局默认项目；显式 `--root` 与 `--workspace` 互斥。全局记录使用原子替换和进程锁，损坏时 fail-closed，详见 [`ADR-0003`](adr/0003-zero-friction-global-home.md)。

## `dyro.toml`

核心配置位于工作区根目录。`repositories.<id>` 是唯一的仓库注册表；所有开发线、任务 worktree、doctor 与 status 从它动态推导，禁止在启动脚本中重复硬编码仓库清单。

```toml
schema_version = 1

[workspace]
name = "example"

[layout]
anchors = "repositories"
lines = "versions"
hotfixes = "hotfixes"
tasks = "worktrees"

[policy]
default_base = "main"
task_branch_prefix = "task/"
allow_push = false
# 事务合并的强制不变量；schema_version 1 不允许设置为 false。
require_clean_merge = true
# local：由当前机器按 adapter 执行；external：本机只允许 dry-run，
# 真实执行、门禁、复核与合并必须交给受控的外部 runner。
execution_mode = "local"
# true 时，receipt-bound PASS review 会进入 review_pending_signoff，
# 需 `dyro task signoff <id> --by <approver>` 才能完成。
require_external_signoff = false
# 显式签名策略；启用后 trust store 为空也 fail-closed。
require_signed_execution = false
require_signed_review = false
require_signed_signoff = false

[repositories.api]
path = "repositories/services/api"
mount = "services/api"
remote = "git@example.com:group/api.git" # 可选；仅供 bootstrap clone 缺失 anchor
# 示例：这是受控项目自己的 gate；按该项目的工具链替换，
# 并非 Dyro 源码仓库的测试命令。
verify = [["python3", "-m", "pytest", "-q"]]

[adapters.codex]
launch = ["codex", "-C", "{workspace}"]
read = ["codex", "exec", "--sandbox", "workspace-write", "{prompt}"]
write = ["codex", "exec", "--sandbox", "workspace-write", "{prompt}"]
```

可用占位符仅为 `{workspace}`、`{root}`、`{task}`、`{line}`、`{prompt}`。命令必须写为 argv 数组；Profile 若通过 `sh -c` 等方式绕过这一约束，安全责任由 Profile 维护者承担。

日常 Profile 维护不必直接编辑此文件：交互式 `dyro setup` 会先预览本地仓库发现、缺失 anchor clone 与首条开发线计划，确认后才应用；`dyro next` 会给出当前唯一安全下一步。`dyro config get/set` 只允许修改经过校验的常用策略，`dyro agent add/test` 可登记预设或 argv adapter 并检查其可执行文件。

`read` adapter 需要写出 `review.md`，因此部分 Agent 不能使用完全只读的进程沙箱。Core 会在复核前后重新核对每个任务仓库的 clean 状态与固定 HEAD；任何源码变动都会使复核失败，不能进入 `done`。

## 运行态

不把每次运行产生的状态塞回主配置。DyroEngineeringFlow 在工作区中使用：

```text
.dyro/
  lines/<id>.toml       功能开发线登记（逐仓 base 与 storage mode）
  hotfixes/<id>.toml    Hotfix 登记
  tasks/<id>/task.toml  任务机读契约
  tasks/<id>/handoff.md 人类规格
  tasks/<id>/evidence-imports/<attempt-id>/ 不可变外部证据世代
  tasks/<id>/current-evidence.json 原子切换的当前证据指针
  tasks/<id>/review.md  独立复核裁决（绑定 receipt 与 task-heads）
  tasks/<id>/signoff.json 外部签收记录（可选策略）
  tasks/<id>/execution-run.json 本地执行 run 身份与最新 attempt 序号
  tasks/<id>/attempts/<attempt-id>.json 本地执行计划、契约哈希与结果
  changes/<id>.toml     跨仓交付 Change Set（逐仓不可变 HEAD）
  decisions.toml        决策点（blocked_on）
  ledger.jsonl          追加式审计台账
```

状态文件以同目录临时文件加 `rename` 原子替换；任务状态、领取记录和签收在任务锁内更新，台账追加在工作区锁内刷盘。调度锁会把“可执行判断 → 状态预留”串行化；外部模式中已领取的 `assigned` 任务也占用其冲突组。因此中断不会留下半份 TOML、JSON、回执或状态文件，也不会让两个遵守 Dyro 协议的进程同时领取或启动同一任务。

状态机为：

```text
backlog → assigned → in_progress → review ───────────────→ done
                         ├→ waiting_answer → in_progress       ▲
                         ├→ failed → assigned                   │
                         └→ review_pending_signoff ─────────────┘
```

非法状态跳转会被拒绝。`task status --force` 只能用于非质量门的受控恢复，不能把任务推进到 `review`、`review_pending_signoff` 或 `done`；这些状态只能由已验证执行证据、独立复核和（如要求）签收的私有流程写入。`task merge` 还会在合并前重新验证当前 task HEAD 所绑定的 PASS review 与签收，状态文本本身不构成放行证据。

## 显式任务图

`task.toml` 仍是任务契约的唯一事实来源。Core 会按开发线把任务清单编译为只读 `TaskGraph`，而不是维护第二份容易漂移的中心图配置：

- Task 是主要节点，`depends_on` 是有向硬依赖边。
- `blocked_on` 将任务连接到人工决策节点。
- `conflict_group` 是调度资源约束，不是依赖边；它只限制同一时刻可运行的节点。
- executor、gates、reviewer 与可选 signoff 构成每个 Task 内部的固定交付子流程。

图命令不会执行 Agent、Git 写操作或门禁：

```bash
dyro task graph --line release-2026-10
dyro task graph --line release-2026-10 --format json
dyro task graph check --line release-2026-10
dyro task explain API-101
```

`task graph check` 拒绝重复任务或边、自依赖、缺失依赖、跨开发线依赖、缺失决策点与依赖环。Mermaid 与 JSON 输出来自同一次图编译；JSON 将冲突组保存在 `constraints.conflict_groups`，不会制造虚假的先后顺序。`task explain` 使用当前任务状态、依赖状态、决策状态与活跃冲突组解释任务为何可调度或阻塞。

`task next`、`task loop` 与 `task daemon` 消费同一个确定性调度计划。每轮计划先一次性读取 TaskGraph、决策、任务状态与有效 claim，形成不可变调度快照；依赖集成检查按依赖任务缓存，不再为每个候选任务重复扫描全图。计划器先计算不占用同波次资源的完整 ready set；daemon 再按 `--parallel` 和 `conflict_group` 选出本轮 wave。这样交互式选择、串行单遍执行和并行执行共享相同的依赖、决策与活跃冲突判断，同时不会把同一冲突组的两个任务放入同一个并行 wave。

调度入口先执行完整 Graph 校验，非法图会 fail-closed，不会只在 `task graph check` 中旁路告警。依赖任务即使已经是 `done`，其 `task-heads.json` 中的每个提交也必须是所属开发线当前 HEAD 的祖先；尚未执行 `task merge` 的完成任务不能释放下游。

本地 `run_task` 在任务执行锁内先完成状态与资源预留，再为任务创建稳定 `run_id` 和递增的 `attempt_id`，因此预留失败不会留下虚假 attempt。每个 attempt 保存原始 `task.toml` 的 SHA-256、规范化调度计划的 SHA-256、当时的 ready set、阻塞原因、直接依赖与决策状态，以及完成结果或异常。dry-run 不创建 provenance；重试继续使用同一 `run_id`。`task answer` 续跑会创建带 `parent_attempt_id` 和答案哈希的子 attempt。进入 review 的 attempt 会被明确写为 `review_attempt_id`，之后失败或未完成的重试不会凭时间戳污染复核目标。`dyro task attempts <id>` 提供只读摘要；`dyro task binding <id>` 输出可直接写入 `review.md` 的完整 `attempt_id` 与 `plan_sha256`。完整记录保存在任务目录并同步写入 ledger。外部 runner 仍使用独立证据包契约，本地 attempt 文件不会被冒充为外部执行证据。

## 外部隔离执行契约

当 Profile 使用 `execution_mode = "external"` 时，Dyro 不会在控制机上启动 Agent、执行 gates、复核或合并。受信任的 runner 通过显式证据接口与控制面交接：

```bash
dyro task claim TASK-42 --by runner-2026-10-01 \
  --key-id runner-2026 \
  --output /secure-transfer/TASK-42.core-claim.json
# runner 在它自己的隔离工作区执行任务，并将声明的 gates、日志和精确 HEAD 打成一个包
dyro task evidence build TASK-42 \
  --workspace /runner/workspace \
  --receipt /runner/out/receipt.md \
  --output /runner/out/TASK-42.zip
# 控制面验证安全 ZIP 路径、哈希、门禁与 HEAD 后导入
dyro task evidence execution TASK-42 --bundle /runner/out/TASK-42.zip
# 独立复核也在隔离环境完成
dyro task evidence review TASK-42 --file /review/out/review.md
dyro task signoff TASK-42 --by release-manager  # 仅 require_external_signoff = true 时
```

领取记录与状态转换由任务锁保护。claim 默认是一小时的有限租约，可用 `task claim-renew` 续租或 `task claim-release` 主动释放；未过期 claim 只能由原 runner 操作，过期后允许新 runner 原子接管并递增 generation。只有有效 claim 才占用 external 冲突组，旧版没有过期字段的 claim 为保持兼容仍视为永久有效。`task evidence build` 不执行 shell 字符串，只按任务中声明的 argv gates 运行，并且会在打包前重新验证隔离工作区的任务分支、clean 状态和逐仓 HEAD。它生成的 ZIP 只允许 `receipt.md`、`gates.json`、`task-heads.json`、`provenance.json` 与 `gates/*.log`，导入端拒绝绝对路径、路径穿越、符号链接、重复文件与超大包。执行证据中的 `gates.json` 使用以下通用格式；每个声明的任务 gate 都必须存在、退出码为 0、日志位于 JSON 同目录内，并且日志哈希必须匹配：

```json
{
  "schema_version": 1,
  "task_id": "TASK-42",
  "receipt_sha256": "<sha256 of receipt.md>",
  "gates": [
    {
      "name": "unit",
      "exit_code": 0,
      "log": "unit.log",
      "log_sha256": "<sha256 of unit.log>"
    }
  ]
}
```

`task-heads.json` 固定执行后实际接受复核的代码：

```json
{
  "schema_version": 1,
  "task_id": "TASK-42",
  "line": "release-2026-10",
  "branch": "task/TASK-42",
  "repositories": {
    "api": "<full Git object id>",
    "web": "<full Git object id>"
  }
}
```

新生成的证据包还包含 `provenance.json`，记录外部 run/attempt、任务契约哈希、由控制面重建的规范化计划，以及 receipt、gates、task-heads 的 SHA-256 闭包；导入端会重新计算并验证这些内容。相同 attempt 的重复提交只有在规范化记录逐字节一致时才幂等成功。缺少 provenance 的旧证据默认拒绝，迁移时必须显式传入 `--allow-legacy`；控制面会为它合成带 `legacy_provenance` 标记的 attempt，且仍不能绕过后续 review binding。

Ed25519 信任根位于 `.dyro/trust/ed25519/execution/`、`.dyro/trust/ed25519/review/` 与 `.dyro/trust/ed25519/signoff/`，三个用途使用不同签名 domain，不能跨用途重放。signature envelope 包含 algorithm、purpose、key ID 与 Base64 signature，签名消息是固定 domain 前缀加去除 signature 后的 RFC 8785 JCS bytes；跨语言 runner 必须按该标准复现消息。私钥必须位于工作区外，只通过命令行显式传入，且权限不能宽于 `0600`；工作区仅保存 PEM 公钥。是否强制签名只由 `require_signed_execution`、`require_signed_review`、`require_signed_signoff` 决定；策略启用后，即使 trust store 为空也 fail-closed，未签名记录和 `--allow-legacy` 都不能绕过。signed execution plan 绑定当前 claim ID、generation、runner 与 execution key ID，过期 claim 的延迟证据无法在接管后重放。独立 reviewer 使用 signed review JSON 认证完整 review 原文和 reviewer 身份。公钥通过原子 create-if-absent 安装，可设置 `not_before`/`not_after`，撤销时保留原公钥并创建不可变撤销记录；trust/revoke 事件写入 `.dyro/trust/ed25519/audit.jsonl`。

`dyro key audit-sync` 将本地 trust 审计日志构造成增量 SHA-256 链，以独立的 `audit-export` domain 签名批次，并要求远程 Witness 返回 `audit-receipt` domain 的签名回执。客户端在 POST 前原子持久化含随机 request ID 的 pending 批次，失败后只重放相同规范字节；即使没有新事件也会发送签名 checkpoint，避免本地日志与状态同时删除后静默回到 genesis。Witness 的幂等缓存仅能在缓存结果仍等于当前 durable checkpoint、receipt key、recovery key 与单调 key epoch 时返回，任一状态已经前进后必须拒绝旧批次。客户端只在验证回执后原子保存最新序号与链头，后续同步会拒绝本地历史回滚或前缀分叉；Witness 必须验证连续序号并按 RFC 8785 字节独立重算新链头，拒绝所有重定向，并将批次和回执写入 WORM 或启用 object lock 的存储。receipt key 轮换由旧 key 或独立 `audit-recovery` key 签署 checkpoint-bound transition，recovery key ID 与前后 key epoch 都受批次与回执签名保护，再由新 key 签回执完成原子切换。协议与部署边界见 [`audit-witness-protocol.md`](audit-witness-protocol.md)。

`dyro witness serve` 是协议的独立服务实现。它以同目录临时文件、fsync、原子 create-only 发布 batch/receipt record，再原子发布 checkpoint；相同批次可在进程崩溃后恢复，只有 record 的签名回执与当前 checkpoint、key、recovery key 和 epoch 全部一致时才返回缓存。服务将可变 checkpoint `storage-root` 与 create-only `record-archive-root` 分开，后者可挂载到独立 WORM/Object Lock 或异域追加式备份；详见 [`witness-deployment.md`](witness-deployment.md)。

导入端只读取一次 receipt、gates、日志、HEAD 与 provenance，再把已验证字节及 attempt/run-state 写入 `evidence-imports/<attempt-id>/`。逐文件与目录 `fsync` 后发布只读不可变世代，最后只用一次原子替换切换 `current-evidence.json`；进程崩溃最多留下未引用世代，读侧始终解析一个完整世代，并按 manifest 哈希验证每个文件。没有指针的旧任务仍兼容读取根目录证据。`task evidence generations` 默认只读列出世代；清理必须显式使用 `--prune --yes`，并同时受 `--older-than-days` 与 `--keep` 约束，当前世代永不进入清理计划。复核文件首行仍为 `verdict: PASS` 或 `verdict: FAIL`，并且必须同时包含 `receipt_sha256: <hash>`、`task_heads_sha256: <hash>`、`attempt_id: <id>` 与 `plan_sha256: <hash>`；任一不匹配时任务继续保持在 `review`。外部签收会再次验证当前 review 与 `review_attempt_id`/plan 的绑定，并把同一绑定写入 `signoff.json`。外部执行返回 `QUESTION` 后，原 claim 保持有效；`task answer` 记录答案哈希并把任务恢复为 `assigned`，供同一 runner 提交下一份证据。升级前已经处于 review 且没有 execution attempt 的历史任务仅沿用旧的 receipt/HEAD 绑定。外部 runner 的认证、容器、云平台或审批系统由 Profile 扩展实现，核心只定义不可绕过的交接证据。

## 安全不变量

1. `line create` 与 `hotfix create` 必须验证每个 anchor 是 Git 仓库且干净；每个仓库可单独固定 base ref。
2. 开发线仓库显式使用 `linked-worktree` 或 `anchor-reference`；doctor 会拒绝与声明不一致的 Git 拓扑。
3. `bootstrap` 只 clone 不存在的 anchor；存在但非 Git 的目录会报错，绝不覆盖。
4. Hotfix 必须显式提供 `--base`；工具不会猜测生产分支或 tag。
5. task 分支只能在 `worktrees/<line>/<task>/` 中修改，默认 `task/<id>`。
6. gates 由 CLI 重新执行并落日志，不依赖 Agent 自述；`execution_mode = "external"` 时，本机不执行这些写入或执行动作。
7. DONE 执行必须生成干净任务 worktree 的逐仓 HEAD 证据；PASS review 与外部签收必须同时绑定当前回执和该证据。
8. 本地复核前后都会核对任务 HEAD 与 clean 状态；复核位修改源码或执行后继续提交都会使证据失效。
9. `task merge` 仅接受 `done`，先完成所有仓库预检，再暂存所有本地 merge；任一仓失败时自动撤销本轮已暂存或提交的本地 merge。
10. `--push` 同时受 `policy.allow_push = true` 和命令行显式请求限制；只有全部本地 merge 成功后才开始逐仓 push。Git 本身不提供跨远端原子 push，部分远端失败会写入台账并保留本地合并供人工恢复。
11. Change Set 只记录干净开发线的精确提交组合；`changeset verify` 会拒绝 dirty、分支或 HEAD 漂移。具体发布平台、promotion 与 forward-port 由 Profile 扩展执行并回写其证据。
12. 下游调度不仅要求依赖任务为 `done`，还要求依赖的逐仓 task HEAD 已进入所属开发线；状态完成不能代替代码集成。
13. 外部执行的后续 attempt 必须继承同一 run、递增 attempt number，并绑定前一 attempt 与回答摘要；同一编号不可被不同证据重写。
14. 任何界面或宿主投影不得把 decayed / inconclusive Proof 显示为通过。
15. Capability Card 缺少 `cannot_prove` 时，默认至少包含 `done` 与 `merge`。
16. Host Compiler 的输出哈希必须可重算；手改投影在 doctor 中失败，不在运行时「尽量兼容」。
17. Proof Bundle 不得包含工作区绝对路径、remote URL 中的凭据、adapter 环境、prompt 或 answer。
18. `verify-bundle` 在缺 procedure、缺 substrate、缺调用方 git 对象、或缺已声明的签名密钥时返回 `inconclusive`，退出码与 `live` 区分。捆内不塞 git 对象。该 `live` 是完整性结论，不是「现在能否 merge」。
19. 发现到的未审计命令不得写入可执行 Card，也不得被 Objective 自动选中。
20. 密钥缺席：Card 不得声明看起来像秘密的环境变量名；需要认证的工具使用用户已登录的本机 CLI 会话，或独立的本机经纪，不把 token 写进 Profile。

## 与 Graph Engineering 的关系（可选读）

行业里有时把「多节点 + 路由/并行 + 校验/停机」的 agent/工作流拓扑称作 **Graph Engineering**（相对单 agent 的 loop）。

Dyro 的交付拓扑与之**实质相近**：TaskGraph（`depends_on` / conflict_group）、任务状态机、gates、独立复核、signoff、merge，以及可选的 `dyro dispatch` 建议性子图，都是可设计、可版本化的工作图节点与边。`dyro task graph` 与文档中的 Mermaid 图是该拓扑的显式视图。

但产品身份仍是 **本地优先的多仓交付控制面（delivery control plane）**，不是 agent 编排框架，也不是 Knowledge Graph / GraphRAG 检索栈：

- 交付真相在 Core：gates 与 receipt/HEAD 绑定的 review（及可选 signoff），不是 agent 自报或多模型投票。
- `dyro dispatch` 输出仅为**建议**，不能替代确定性 gate 或独立复核。
- 业务规则留在 Profile；Core 不绑定某家模型或协作品牌。

因此文档主叙事继续用 TaskGraph / gates / evidence；Graph Engineering 仅作概念对照，不作产品更名。

## 扩展路线

未来的 adapter、通知、签名规则、发布平台与审批系统应使用 Python entry point 或独立 Profile 扩展包接入；不要把某个组织的策略加入 core 默认行为。

`0.7.0` 把已有证据物理学抽成可复验的 Proof，并把 argv adapter 升级为 Capability Card，再把定律编译为只收缩权威的宿主投影。衰减与现有 merge / 下游检查同真值；`proof verify` 看当前工作区，`verify-bundle` 只核完整性，两套结论不得混称。Console summary 与 `dyro objective attention` 不是同一套 Proof 展示。Console 独立 inspect、`trigger_observation` 与可携带核验门禁已在 `0.7.1` 落地。`0.7.2` 把换工具后的开场白收成事项加一条只读下一步，不另开会话层。`0.7.3` 把 Console 空关注收回未读，并不再在首页和下一条修复命令里漏本地路径。后续功能继续在 `0.7.x` 开发和上线，不另开 `0.8.0` / `0.9.0` / `1.0.0` 功能号。可携带核验是 Proof Bundle 加调用方提供的 git 对象，核验完整性而不是身份，也不承诺与当前 merge 同一套 `live`。这不另造 TaskGraph 或完成状态机；见 [`交付物理学`](designs/delivery-physics.md) 与 [`ADR-0006`](adr/0006-delivery-physics-and-capability-plane.md)。

开发者侧的可选本地多 Agent 派发（五段式任务契约、注入前机密守卫、locator 核验、隔离 patch）与上述控制面分层并列，随 `dyro` 安装包分发（`dyro dispatch` / `import experiments.local_agent_dispatch`），但**不**替代 gates/合并。同时写多块走 Core Peer Wave（task worktree + `conflict_group`），见 [`peer-wave-execution.md`](designs/peer-wave-execution.md)、[`ADR-0002`](adr/0002-optional-local-agent-dispatch.md)、[`多智能体编排纪律`](agent-orchestration-discipline.md) 与 [`可选本地 Agent 派发设计`](designs/optional-local-agent-dispatch.md)。
