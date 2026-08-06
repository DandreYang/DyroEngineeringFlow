# Dyro Agent Bridge Design Adversarial Review Board

Date: 2026-08-06

Scope:

- Repository: `/Users/dandre/DyroProjects/DyroEngineeringFlow/versions/dev/dyroengineeringflow`
- Review substrate: `feat/dev@d00d5ca6f1f64edc606ca23d44018033f76f67f4`
- Material under review: the conversation design titled `Dyro Agent Bridge v1`
- Review mode: design and plan review; no business-code implementation

Reviewed Materials:

- `docs/architecture.md`
- `docs/designs/optional-local-agent-dispatch.md`
- `docs/adr/0002-optional-local-agent-dispatch.md`
- `docs/adr/0003-zero-friction-global-home.md`
- `docs/adr/0004-native-continuation-engine.md`
- `src/dyro/cli.py`
- `src/dyro/hub.py`
- `src/dyro/workspace.py`
- `src/dyro/tasks.py`
- `src/dyro/continuation/`
- `experiments/local_agent_dispatch/`
- `pyproject.toml`

SSOT:

- Current source at the locked review substrate above outranks the proposed design.
- Existing delivery invariants in `docs/architecture.md` remain fixed unless current source disproves them.
- Existing `dyro dispatch` remains outbound and advisory; the proposed `dyro bridge` is inbound.

## Rules

1. Each reviewer writes only in their own signed section.
2. Conflicts are resolved by current source or reproducible runtime behavior.
3. Unprovable claims are marked `须人工核`.
4. Findings use P0/P1/P2 severity.
5. Reviewers must try to refute the proposed design, not optimize toward agreement.
6. No reviewer may edit another reviewer's section or Final Arbitration.
7. Product preferences are not treated as security enforcement.

## Fixed Decisions

- Dyro Core remains the sole delivery control plane.
- Skill text is guidance, never an authorization boundary.
- `dispatch`, Bridge, Plugin, and MCP cannot review/signoff/merge/push on advisory Agent output.
- Commit, push, merge, signoff, release, publish, and cleanup remain separately authorized.
- Read-only operations must be side-effect free.

## Open Decisions

1. Whether Phase 1 should introduce a generic `bridge invoke` operation or only typed commands/tools.
2. Whether MCP belongs in the Dyro wheel as an optional extra or in a separately versioned integration package.
3. Whether any R1 apply operation should be exposed in v1, or v1 must remain inspect-and-plan only.

---

# Architecture Review Section

## Signed review

- Reviewer: Turing
- Reviewed at: 2026-08-06 18:43:02 +0800
- Substrate verified: `feat/dev@d00d5ca6f1f64edc606ca23d44018033f76f67f4`
- Verdict: **NO-GO for the proposed v1 R1 `apply`; CONDITIONAL GO only for a revised inspect-and-plan v1.**
- Runtime check: `.venv/bin/python -m unittest tests.test_continuation_supervision tests.test_workspace tests.test_hub` passed 74 tests. This confirms the existing Objective confirmation path and current workspace/home behavior; it does not prove the proposed generic Bridge contract.

## Executive challenge

The direction—an inbound, structured Agent interface distinct from outbound `dispatch`—is sound. The proposed layering is not yet sound enough to mutate state. In the current source, “Core” is not a transport-neutral application service: policy checks, confirmation rules, rendering, and even some mutations live in `cli.py`. Adding an `Operation Registry` that owns risk, policy, and handlers alongside that CLI would create the second control plane the design says it avoids. More importantly, a confirmation digest prevents some stale-plan races, but it does not provide an idempotency or crash-consistency boundary. The only current implementation with those properties is the Objective-specific Action Journal, and it is deliberately coupled to Objective leases, budgets, Task operations, and uncertainty handling rather than being a generic transaction engine.

The safe cut is therefore:

```text
Codex/Claude
  -> typed MCP tools OR one schema-validated JSON inspect/plan endpoint
  -> Bridge Exposure Catalog (metadata only)
  -> Core application services (policy + authoritative plan/apply)
  -> existing domain locks/journals/stores
```

For v1, stop before the final arrow can mutate. Do not expose R1 apply until each operation has a Core-owned linearization point and operation-specific recovery semantics.

## P0 findings

### P0-1 — The proposed Operation Registry would become a second authorization/control plane

**Claim refuted:** “Skill, CLI, and MCP share one Operation Registry” is not sufficient to preserve Core as SSOT when that registry also owns risk class, permission policy, and plan/apply handlers.

**Evidence:**

- Current confirmation policy is CLI-local: `_require_yes` and `_require_objective_yes` enforce different rules in `src/dyro/cli.py:269-280`.
- Line creation defaults and mutation dispatch are assembled in `src/dyro/cli.py:1650-1679`, not in a transport-neutral command object.
- `task.create` is implemented directly in the CLI, including locking and two-file persistence, at `src/dyro/cli.py:1729-1751`; there is no equivalent Core service to reuse.
- Task execution policy and state fencing remain in the task APIs (`src/dyro/tasks.py:775-830`, `src/dyro/tasks.py:1330-1367`), while Objective mutation authority is separately enforced by its store and Action Journal (`src/dyro/continuation/store.py:646-680`, `src/dyro/continuation/supervision.py:365-510`).

If Bridge independently decides that an operation is R1 and may apply, while the human CLI keeps its current checks, the two surfaces can drift even if both point at some of the same functions.

**Required fix:** Rename and narrow the registry to an **Exposure Catalog**. It may contain operation ID, schemas, maximum risk, protocol versions, and a reference to a Core application service. It must not be the owner of authorization or mutation invariants. Extract typed Core services first; both CLI and Bridge must eventually call those services. Until a mutating CLI command has been migrated, Bridge must expose it only as inspect/plan or not at all.

### P0-2 — `task.answer` is materially misclassified as R1

**Claim refuted:** The proposed R1 list treats `task.answer` as a local, recoverable control write.

**Evidence:** In a local-execution Profile, `answer_task` takes the execution lock, reserves the task, creates an execution attempt, and invokes `_answer_task` (`src/dyro/tasks.py:1558-1606`). `_answer_task` can create worktrees, launch the configured Agent argv, capture output, execute gates, and change quality state (`src/dyro/tasks.py:1609-1643`). Only the external-execution branch records an answer without launching the local Agent (`src/dyro/tasks.py:1559-1575`).

Thus risk is contextual, and the maximum authority of `task.answer` is execution-write, not control-write. A static R1 declaration could let a generic apply tool start an Agent and gates under an authorization presented as a metadata update.

**Required fix:** Remove `task.answer` from R1. Mark the catalog entry with maximum risk R2 and compute an `effective_risk` in the Core plan from the loaded Profile. Keep all variants plan-only in v1; later expose separate typed operations such as `task.record_external_answer` and `task.resume_local_execution`, each retaining current task/execution locks and policy checks.

### P0-3 — The proposed confirmation payload does not bind the actual operation read set or implementation version

**Claim refuted:** Hashing workspace identity, config digest, repository HEAD/dirty state, line list, inputs, and effects is enough to make a generic apply stale-safe.

**Evidence:** Line planning also reads target-root emptiness, anchor Git validity, the resolved base ref, destination absence, branch existence, base-to-branch ancestry, and (for `anchor-reference`) the currently checked-out branch (`src/dyro/workspace.py:249-296`). A base or pre-existing branch ref can move while the anchor `HEAD` and dirty state remain unchanged. The proposed generic snapshot does not explicitly bind those resolved refs or predicates. The Objective digest succeeds because it manually serializes every safety-relevant fact for that one domain (`src/dyro/continuation/supervision.py:110-166`) and apply rebuilds the whole wave plus each action (`src/dyro/continuation/supervision.py:378-405`). That is evidence for operation-specific confirmation, not evidence that the pattern can be generalized by one fixed snapshot.

There is also no planner/operation revision in the proposed hash. A plan copied across a Dyro upgrade could retain the same visible effects while the handler semantics changed. Current JCS support itself is real (`src/dyro/canonical.py:3-17`, dependency at `pyproject.toml:11-14`), so RFC 8785 encoding is not the blocker; defining the complete semantic payload is.

**Required fix:** Each Core operation must produce a typed, JSON-only `read_set` containing every predicate and resolved object ID it used. Confirmation must bind at least `protocol_major`, `operation_id`, `operation_schema_version`, `planner_revision`, canonical workspace root/config digest, normalized input, `read_set`, and semantic effects. Apply must acquire the operation's authoritative domain lock, rebuild the typed plan under that lock, compare the digest, and only then cross its durable start boundary. Patch upgrades that change planning or apply semantics must bump `planner_revision` and invalidate old confirmations.

### P0-4 — Request IDs and hashes do not supply idempotency, atomicity, or crash recovery

**Claim refuted:** The proposed rule “same request ID + operation + confirmation SHA does not duplicate resources” can be implemented as a generic Bridge feature over current R1 APIs.

**Evidence:**

- `create_line` has no line-creation lock around plan plus apply. It performs multiple Git worktree/branch mutations and writes the line record last (`src/dyro/workspace.py:330-380`). Its recovery is best-effort (`src/dyro/workspace.py:163-206`). A newly created branch (`src/dyro/workspace.py:289-295`) is not removed by that rollback, so the proposed example's blanket `reversible: true` is false.
- `task.create` has a lock, but creates the directory and then two files in sequence (`src/dyro/cli.py:1739-1750`). A crash after `task.toml` leaves a partial directory; replay fails because the directory already exists. No request journal can currently distinguish “not started,” “partially applied,” and “complete.”
- By contrast, Objective apply publishes an intent, then a durable Action-start before invoking the Task API, and records post-start exceptions as `uncertain` (`src/dyro/continuation/supervision.py:418-490`). Its idempotency key binds Objective revision, events, scope, generation, action, and budgets (`src/dyro/continuation/action_models.py:101-135`). These are domain-specific invariants, not available to line/task/workspace mutations.

A success-only ledger appended after mutation cannot close the crash window between the side effect and receipt. Replaying after that window can duplicate or damage state; refusing replay without a receipt can strand a successfully applied operation.

**Required fix:** Keep v1 inspect-and-plan only. Before opening any R1 apply, define per-operation linearization and recovery rather than a universal success ledger:

1. convergent operations may prove idempotency from authoritative state;
2. multi-effect operations need a durable intent/start/receipt journal and `uncertain` terminal state;
3. plan/recheck/apply must run under a declared domain lock and global lock order;
4. recovery must distinguish safe replay, already applied, repair required, and uncertain;
5. `request_id` is correlation only until a durable record atomically binds it to canonical input and confirmation digest.

`workspace.add` is the best first post-v1 pilot because the registry already uses an exclusive lock plus atomic replace (`src/dyro/hub.py:161-168`) and can converge on an existing matching record. `line.create` and `task.create` are not acceptable pilots without redesign.

## P1 findings

### P1-1 — The zero-write machine read path is not yet a reusable Core boundary

**Claim challenged:** Existing read commands can simply be registered as R0.

**Evidence:** Objective plan/tick/attention deliberately call `get_objective(..., recover=False)` (`src/dyro/cli.py:2358-2362`, `src/dyro/cli.py:2404-2423`), but `objective list` and `status` call the default recovery-enabled readers (`src/dyro/cli.py:2332-2348`). Those readers may take the Objective lock and recover a pending transaction (`src/dyro/continuation/store.py:425-441`). Current Git observations also use ordinary `git status` (`src/dyro/workspace.py:383-409`, `src/dyro/process.py:18-50`) without an explicit `GIT_OPTIONAL_LOCKS=0`/`--no-optional-locks` contract; whether a given Git version refreshes index metadata is **须人工核** on each supported platform.

The focused 74 tests passed, but the current no-write tests compare selected content under normal state (`tests/test_cli.py:812-878`); they do not inject pending Objective recovery, trace filesystem syscalls, or prove Git index metadata is untouched.

**Required fix:** Add a transport-neutral Observation facade whose APIs have no recovery/repair behavior, no update check, no recent-item write, and no implicit directory/lock creation. Provide an explicit mutating `repair` operation separately. Run Git observations with optional locks disabled and add pending-state, permission-denied-home, and syscall/file-metadata acceptance tests. Define “side-effect free” as no persistent semantic write plus no created path; do not rely only on brittle whole-tree mtime comparison.

### P1-2 — JSON transport, generic invocation, MCP packaging, and version compatibility need one concrete decision

**Claim challenged:** `dyro bridge` under the existing CLI plus `python -m dyro.bridge.mcp` is already a reliable distribution/compatibility shape.

**Evidence:** The main CLI builds one argparse parser, dispatches command functions that print directly, and catches `DyroError` into decorated text (`src/dyro/cli.py:3618-3647`). Reusing this path cannot guarantee “stdout is exactly one JSON object” for parse and routing failures. The current wheel exposes only the `dyro` script and explicitly enumerates packages (`pyproject.toml:35-56`). A plugin-launched `python -m dyro.bridge.mcp` uses the host's `python`, which need not be the pipx/venv interpreter containing `dyro[mcp]`. The proposal also defines a broad `dyro_apply_confirmed_plan`; adding a newly exposed operation in a newer Core would silently widen what an old host-facing generic tool can execute.

**Required fix and decisions:**

- Permit one schema-validated generic JSON endpoint only for **inspect and plan** in v1; it must route before the human argparse/error renderer or use a dedicated `dyro-bridge` console script.
- MCP must expose typed tools. Do not expose generic `execute`, generic shell, or generic `apply_confirmed_plan`. Future applies get operation-specific typed tools and Core-side maximum-risk enforcement.
- Keep MCP in the same Dyro distribution as an optional extra for v1 to avoid a second release/version matrix, but install a real `dyro-mcp = dyro.bridge.mcp:main` console entry point. The Plugin invokes that executable, not ambient `python`.
- Handshake on protocol major, operation schema version, and planner revision. Unknown majors/operations fail closed; additive response fields are minor-compatible. The apply digest must reject a plan created by an incompatible planner revision.

## P2 findings

No independent P2 finding is recorded in this pass. Schema discoverability, localized messages, output truncation, and Plugin installation ergonomics are useful but should not consume implementation capacity before the P0/P1 boundaries above are closed.

## Open Decisions

1. **Generic invoke vs typed tools:** generic JSON inspect/plan endpoint is acceptable for CLI transport; MCP tools and every future apply remain typed. Generic mutating invoke is rejected.
2. **MCP packaging:** same `dyro` distribution, optional `mcp` extra, dedicated `dyro-mcp` executable, protocol handshake. Reconsider a separate package only after a compatibility policy and release automation exist.
3. **R1 in v1:** none. v1 is inspect-and-plan only. `workspace.add` may become the first separately reviewed R1 pilot; `line.create`, `task.create`, `task.answer`, and Objective execution are excluded.
4. **Registry authority:** adopt an Exposure Catalog owned by the Bridge adapter for exposure metadata; Core typed services remain the only policy and mutation authority.

## Required Fixes before implementation approval

1. Amend ADR-0006 to state the Core-service/Exposure-Catalog split and the inspect-and-plan-only v1 scope.
2. Define typed Observation and Plan models in Core before adding MCP or Plugin packaging; do not call `cmd_*` functions from Bridge.
3. Specify operation-specific `read_set`, `planner_revision`, lock, linearization point, idempotency, uncertainty, and recovery fields. A shared envelope is allowed; shared transaction semantics are not assumed.
4. Add a dedicated machine transport that owns JSON parse/error behavior and cannot run daily update or human rendering.
5. Prove the no-write boundary with pending-recovery and Git-optional-lock cases in a real Codex-style sandbox.
6. Only after the above passes, ship the Skill and typed read-only MCP tools; require a separate review gate before enabling the `workspace.add` R1 pilot.

---

# Security Review Section

## Reviewer: Curie

**Verdict：No-Go。** 当前方案不能开放任何 R1 `apply`，且 R0 清单必须重分级。发现计数：**P0 × 3、P1 × 3、P2 × 0**。以下结论以 `feat/dev@d00d5ca6f1f64edc606ca23d44018033f76f67f4` 当前源码为准；Bridge 尚未实现的协议细节均明确标为 `须人工核`。

### P0-CURIE-01：Confirmation SHA 只证明计划完整性，不构成用户授权

**证据：** 现有 Objective 将 `confirmation_sha256` 原样放进可机器读取的 plan payload（[`src/dyro/continuation/supervision.py:202-214`](../../../src/dyro/continuation/supervision.py)），非交互入口随后只校验调用者同时提交 `--yes` 和相同 SHA（[`src/dyro/cli.py:2438-2471`](../../../src/dyro/cli.py)）。SHA 的计算确实绑定了当前语义事实（[`src/dyro/continuation/supervision.py:110-166`](../../../src/dyro/continuation/supervision.py)），但没有绑定一个模型不可伪造、不可读取的人类批准凭据。

**攻击路径：** 同一编码智能体调用 `plan` 得到 SHA → 不经过用户，直接调用设计中的 `dyro_apply_confirmed_plan` 并回填该 SHA → 在宿主已授予工作区写权限时不会出现新的权限提示。`actor`、`--yes`、复制 SHA 都由同一不可信调用者提供，所以不能证明用户在 plan 之后作过确认。

**Required Fixes：** v1 只能 inspect-and-plan。若以后开放 apply，批准必须由宿主 UI 或独立本地 broker 在模型不可见的通道签发一次性 capability；至少绑定 operation、规范化 input、workspace identity、effects、过期时间、随机 nonce、调用会话和单次消费状态。Confirmation SHA 继续作为完整性摘要，但字段名和文档不得称其为授权。`须人工核`：Codex/Claude 各宿主是否提供真正不回传给模型的 approval token；若没有，R1 永久不进 MCP。

### P0-CURIE-02：`task.gates` 被列为 R0，但当前实现会执行 Profile 声明的任意 argv 并写日志/台账

**证据：** CLI `task gates` 直接调用 `run_gates`（[`src/dyro/cli.py:1956-1962`](../../../src/dyro/cli.py)）；后者展开任务中的 argv、启动子进程、写 `gate-*.log` 并追加 ledger（[`src/dyro/tasks.py:1314-1327`](../../../src/dyro/tasks.py)）。Profile 的 gate 是通用 argv，并非只读内建检查；Core 的架构也明确 gates 属于执行/验证机制，而不是只读图查询（[`docs/architecture.md:203-214`](../../../docs/architecture.md)）。

**攻击路径：** 攻击者在可导入工作区的 `task.toml` 中放置有写入、网络或凭据读取副作用的 gate → Skill/MCP 按 R0 “可自动执行”调用 `task.gates` → 以 MCP Server/Dyro 进程权限执行任意程序并持久化输出。这同时击穿“R0 零写入”和“Plugin 不扩权”。

**Required Fixes：** 从 R0 删除 `task.gates`。拆成 `task.gates.describe` / `task.gates.last_result`（纯读取）与 `task.gates.run`（至少 R2 execution-write）；R0 registry 必须逐 handler 做副作用证明，禁止按命令名称或 `dry_run` 推断。所有可启动 subprocess、读取 adapter、执行 Git 网络操作或写 log/ledger 的 handler 均不得进入 R0。

### P0-CURIE-03：隐藏 R3 MCP 工具不能阻止同一编码智能体绕过 Bridge 直接调用现有 CLI

**证据：** 当前 `task signoff` 允许只传任意 `--by`，签名参数是可选的（[`src/dyro/cli.py:3429-3434`](../../../src/dyro/cli.py)）；local mode 下配置反而禁止启用 `require_signed_*`（[`src/dyro/config.py:218-225`](../../../src/dyro/config.py)），而 `_signoff_task` 只在 external mode 强制签名和 principal 独立性（[`src/dyro/tasks.py:1878-1905`](../../../src/dyro/tasks.py)）。`task merge` 的 CLI 边界仅要求可由 Agent 自行提供的 `--yes`（[`src/dyro/cli.py:269-273`](../../../src/dyro/cli.py)、[`src/dyro/cli.py:2282-2290`](../../../src/dyro/cli.py)）。

**攻击路径：** 安装 Skill/Plugin 后，拥有 shell 和工作区写权限的编码智能体绕过 MCP，直接运行普通 `dyro task signoff ... --by <声称的人>` 与 `dyro task merge ... --yes`；若 Profile 允许 push，还可走现有 CLI 的 `--push`。Skill 文本和“不暴露 R3 工具”对此没有强制力。

**Required Fixes：** ADR 必须先声明威胁模型：同 UID、可执行 `dyro` 的 Agent 不是可通过 Skill 隔离的安全主体。需要 broker/daemon 成为唯一 mutation authority，敏感 Core 操作要求模型不可访问的人类 capability 或外部签名；直接 CLI 也必须走同一 policy enforcement。只要开启 `require_external_signoff`，signoff 就必须使用独立可信 principal，不应因 local mode 降级为自报字符串。达不到该隔离时，只能承诺“防误操作”，不能承诺“Agent 不能 signoff/merge/push”。

### P1-CURIE-04：Plan→Apply 缺少覆盖整个副作用窗口的冲突锁、fencing 与 durable intent，SHA 复算仍存在 TOCTOU/重复执行

**证据：** 当前 line 创建先检查状态/目标/refs（[`src/dyro/workspace.py:209-296`](../../../src/dyro/workspace.py)），随后逐仓创建 worktree，最后才写 line state；整个过程没有 workspace/line mutation lock，崩溃恢复只是进程内 best-effort rollback（[`src/dyro/workspace.py:330-379`](../../../src/dyro/workspace.py)）。相比之下，现有 Objective Action Journal 会先 create-only reserve intent，再在 owner lease/generation 下 start，并把 idempotency key 绑定完整 authority facts（[`src/dyro/continuation/action_models.py:101-135`](../../../src/dyro/continuation/action_models.py)、[`src/dyro/continuation/action_journal.py:309-360`](../../../src/dyro/continuation/action_journal.py)）。

**攻击路径：** 两个不同 `request_id` 对同一 line、不同 repository 子集同时通过 plan → 两边均在 state 尚不存在时开始创建 → 最后一次原子 replace 覆盖 line manifest，遗留另一边 worktree；或进程在首个 Git 副作用后被杀，重试因没有 durable start/receipt 无法区分“未执行”和“执行结果不确定”。另一路径是 plan 固定 `base="main"`，而当前命令最终把符号 ref 交给 `git worktree add`（[`src/dyro/workspace.py:265-266`](../../../src/dyro/workspace.py)、[`src/dyro/workspace.py:289-295`](../../../src/dyro/workspace.py)）；若哈希只记录 anchor 当前 HEAD 而未记录 `main^{commit}`，ref 漂移后仍可能应用不同代码。`须人工核`：设计中的 `repository_heads` 是否意图覆盖每个实际解引用 ref；当前字段定义不足以证明。

**Required Fixes：** 引入 workspace 级 mutation lock + 每资源 conflict key，锁内完成“重载 config/registry → 重算 plan → 消费 approval → durable intent/start → Core effect → receipt”。复用 Action Journal 的 create-only、owner generation 与 uncertain 语义；`request_id` 只能是相关 ID，不能代替幂等键。哈希必须绑定每个 symbolic ref 的 full OID、Git common-dir identity、目标父目录 identity 和精确 effect argv；任何副作用后异常都记录 `uncertain`，禁止盲重试。

### P1-CURIE-05：现有 Core/hub 的路径检查是 pathname/check-then-use，不能满足设计声称的“realpath 在工作区内”安全边界

**证据：** 配置只拒绝绝对路径和 `..`，不拒绝 symlink 路径分量（[`src/dyro/config.py:142-145`](../../../src/dyro/config.py)）；line destination 直接由 `config.root / layout / id / mount` 拼接（[`src/dyro/workspace.py:136-150`](../../../src/dyro/workspace.py)），随后 `mkdir`/Git 会跟随父目录 symlink（[`src/dyro/workspace.py:353-372`](../../../src/dyro/workspace.py)）。通用 `atomic_write_bytes` 与 `exclusive_lock` 也只对最终 lock fd 使用 `O_NOFOLLOW`，父目录仍按 pathname 创建/替换（[`src/dyro/state.py:34-50`](../../../src/dyro/state.py)、[`src/dyro/state.py:200-239`](../../../src/dyro/state.py)）。hub registry 会 resolve 记录中的 root，但读取/替换 registry 仍是“检查终端 symlink后按路径操作”（[`src/dyro/hub.py:83-112`](../../../src/dyro/hub.py)、[`src/dyro/hub.py:161-168`](../../../src/dyro/hub.py)）。Objective store 已有基于 directory fd 的更安全范式（[`src/dyro/continuation/store.py:66-105`](../../../src/dyro/continuation/store.py)）。

**攻击路径：** 在 plan 后把 `versions`、`.dyro/lines`、tasks parent 或 `DYRO_HOME` 的父路径替换为 symlink/reparse point → apply 的 mkdir、临时文件或 rename 被重定向到计划外位置；仅在 apply 前再次 `resolve()` 仍挡不住检查后的替换。registry alias 也没有持久化的 workspace UUID/inode binding，路径被替换后可能指向不同 Profile。

**Required Fixes：** 写侧必须从预先打开且验证过的 workspace/registry directory fd 开始，逐级 `openat/mkdirat` + `O_NOFOLLOW`，并在整个事务中固定 `(st_dev, st_ino)`；Windows 无等价安全实现时 fail-closed。禁止直接把现有 line/task/hub 写 handler 包进 Bridge。为 workspace 引入稳定 identity，并在 plan/apply 同时绑定 alias、canonical root、config hash、root/config inode 与 registry generation；不匹配即 stale。

### P1-CURIE-06：`actor`/`request_id` 是不可信自报，原始 Core 错误又可能把敏感 argv/stdout送入 MCP 与审计

**证据：** 设计已说明 `actor` 不是凭据，却拟把 `actor_kind`/`host` 写入 apply ledger；这会形成看似可信的归因。当前真正的 external signoff 会验证签名 key、principal 与 execution/review 身份独立性（[`src/dyro/tasks.py:1073-1095`](../../../src/dyro/tasks.py)），说明自报字符串不能承担身份。另一方面，通用 `require_ok` 会把完整 argv 和合并后的 stdout/stderr写入异常（[`src/dyro/process.py:37-57`](../../../src/dyro/process.py)）；local dispatch 已专门在任务文本、Provider 输出和持久化错误前执行 secret guard/redaction（[`experiments/local_agent_dispatch/task_contract.py:63-69`](../../../experiments/local_agent_dispatch/task_contract.py)、[`experiments/local_agent_dispatch/context_guard.py:80-105`](../../../experiments/local_agent_dispatch/context_guard.py)），Bridge 方案尚未把同等规则列为强制边界。

**攻击路径：** 调用者伪造 `actor.host="codex"` 与任意 request ID，使 ledger 看起来像某宿主/用户批准；同时让 Git/ref/路径或下游工具在错误中回显含 token 的输入/remote URL，MCP 将 error details 或 stderr 返回远端模型并可能再次落审计。

**Required Fixes：** 审计区分 `claimed_actor` 与由 transport/broker 观测到的 `authenticated_principal`；没有 approval credential 时明确写 `authorization=unverified`，不得记录“用户已确认”。event ID 由服务端生成，request ID 只作 correlation。所有请求字符串、Core 异常、argv、stdout/stderr、warning、MCP response 和 audit field 统一做大小上限、凭据检测和不可逆脱敏；日志默认不含绝对路径、原始 prompt、remote URL query/userinfo 或环境变量。

### Go / No-Go 与解除条件

**当前：No-Go（Bridge v1 的 MCP R1 apply、任何 R2/R3、以及原方案 R0 清单）。** 允许继续实现的唯一范围是：typed、零 subprocess、零 lock 创建、零 mtime/ledger 变化的 inspect API，以及返回不可执行计划的 plan API。

转为有限 Go 前必须同时满足：P0-CURIE-01 的模型不可见批准能力已由至少一个真实宿主端到端证明；`task.gates` 等所有 handler 完成代码级副作用分类；普通 CLI 不再成为旁路；P1 的 mutation journal/fencing、fd-relative 路径、workspace identity、secret redaction 和可信审计语义均有故障注入/并发/真实沙箱测试。若宿主无法提供不可见批准能力，最终决策应选择 Open Decision 3 的“v1 inspect-and-plan only”。

— **Reviewer: Curie**

---

# Product, Skill, Plugin, and Evaluation Review Section

## Reviewer: Shannon

### Verdict

**整体 No-Go；只允许收缩后的 R0 inspect-and-plan 切片进入实现。** `dyro bridge` 作为入站、机器可读适配层有真实价值，而且现有 Core 已经有可复用的纯读取解析器；但当前方案同时承诺 R1 apply、Codex Plugin、MCP 和跨宿主安装，授权来源、制品分发、版本握手与真实沙箱证据均未闭环。若照原方案实施，最危险的结果不是“命令不可用”，而是把已有执行型命令误包装成 R0，或让 Agent 自己取得 Confirmation SHA 后再自行 apply。

本轮锁定并核验 `feat/dev@d00d5ca6f1f64edc606ca23d44018033f76f67f4`。正向证据是：当前 resolver 已实现“显式 alias → 当前目录向上发现的 Profile → registry 默认/唯一可用工作区”的无副作用解析（[`src/dyro/continuation/resolution.py:61-93`](../../../src/dyro/continuation/resolution.py)），registry 的缺失读取不创建目录、损坏时 fail-closed（[`src/dyro/hub.py:45-64`](../../../src/dyro/hub.py)、[`src/dyro/hub.py:105-140`](../../../src/dyro/hub.py)）。本轮用隔离 `DYRO_HOME` 登记默认工作区后，从无关 `/tmp/.../unrelated` 运行 `dyro task list` 成功，`workspaces.json` 的 mtime/size 前后相同。因此 Bridge 应复用该 resolver，不能再造一套发现规则。

### Findings（最多六项）

#### P0-1 — R0 清单已把真实执行面误标为只读

方案把 `task.gates` 放入 R0，但当前同名 CLI 不是“查看 gate 定义”：它直接调用 `run_gates`（[`src/dyro/cli.py:1956-1962`](../../../src/dyro/cli.py)）；后者逐项启动 gate argv、写 `gate-*.log`，并向 ledger 追加结果（[`src/dyro/tasks.py:1314-1327`](../../../src/dyro/tasks.py)）。这是当前源码对设计的直接反证。即便计划另写 handler，操作名仍会让 Skill、Plugin 作者和使用者错误复用现有语义。

**Required fix：** 删除 R0 `task.gates`，拆成 `task.gate_definitions.get`（只读配置投影）与至少 R2 的 `task.gates.run`。每个 OperationSpec 在进入 allowlist 前必须提交实际调用图、副作用清单，以及能令 `run`、`ledger`、`atomic_write_text`、`mkdir`、网络和 Agent spawn 一触发即失败的负向测试；不能按命令名称人工分级。

#### P0-2 — Confirmation SHA 绑定了计划，却没有绑定可信的“用户确认”

请求中的 `actor` 明确只是非可信审计字段，但方案没有另一个可信授权来源。同一个 Agent 可以先调用 plan 得到 SHA，再自行调用 `dyro_apply_confirmed_plan`；SHA 能防 stale plan，不能证明用户在宿主 UI 中确认过。`--yes`、聊天中的一句“确认”或 Skill 纪律也都不是 Core 可验证的 authority。更严重的是，本地 MCP server 是否继承 Codex/Claude 的 workspace sandbox 与逐工具批准策略，仓库中没有实现或证据，**须人工核**；OS `PermissionError` 也不能预先等价为宿主授权状态。

**Required fix：** v1 只开放 inspect 和 plan；MCP 不注册通用 apply 工具。R1 先保留为人类 CLI 的独立后续动作。未来若开放，必须定义宿主可验证的一次性授权凭据或强制的 host-native approval broker，绑定 `operation + confirmation SHA + workspace identity + expiry + single use`，并在每个支持宿主的真实进程边界上证明 server 不会越过宿主权限。证明完成前，Open Decision 3 裁定为 **inspect-and-plan only**。

#### P1-1 — Plugin/MCP 没有可安装、可升级、可回滚的制品闭环

当前 wheel 只显式包含 Python packages，package-data 只有 Console assets（[`pyproject.toml:41-59`](../../../pyproject.toml)）；sdist manifest 也只列文档、示例和 Console assets（[`MANIFEST.in:1-8`](../../../MANIFEST.in)）。方案把 Plugin 放在 `integrations/codex/...`，却没有让 wheel/sdist 包含该目录，也没有落实先前提出的 `dyro integration install codex|claude`、卸载、覆盖冲突、原子升级和失败回滚。当前 CI 的制品 smoke 只验证 dispatch/continuation/Console（[`.github/workflows/ci.yml:52-90`](../../../.github/workflows/ci.yml)），不会发现 Plugin 或 Skill 丢包。Core 的更新流程只验证 Python distribution 版本（[`docs/updates.md:42-56`](../../../docs/updates.md)），宿主目录中已复制的 Plugin 会产生版本漂移。

方案中的“版本握手”也只有 `bridge_version`/`dyro_version` 展示，没有 client/integration 版本、支持的 schema 范围、协商结果、capabilities digest 或 major mismatch fail-closed 规则。现有仓库只证明 Codex/Claude 等工具可被发现或启动（[`src/dyro/tooling.py:63-145`](../../../src/dyro/tooling.py)），这不证明 Claude/Cursor 能消费 Codex Plugin。非 Codex 宿主均 **须人工核**。

**Required fix：** v1 明确为 Core CLI + host-neutral Skill source，不宣传跨宿主 Plugin。Core Bridge 随 `dyro` wheel；Codex Plugin/MCP 若进入下一阶段，应成为单独版本化制品，声明兼容的 Core/schema 区间，并提供 `integration status/install/update/uninstall --dry-run`、文件 ownership manifest、原子替换与回滚。CI 必须从 wheel 和 sdist 外部安装，逐字验证 Skill/Plugin/MCP 资源与握手的 N/N-1、Core-newer、Plugin-newer、缺少 `[mcp]` 四种状态。

#### P1-2 — “任意目录发现”缺少完整的用户流与错误恢复契约

现有 Core 对 malformed local Profile 明确拒绝回落到 registry 默认，避免悄悄操作错误项目；已有测试覆盖损坏文件、悬空 symlink 和目录替代文件（[`tests/test_continuation_resolution.py:41-92`](../../../tests/test_continuation_resolution.py)）。方案只写了 `workspace.resolve` 和“从任意目录”，没有冻结 resolver precedence、选择来源字段，或零/多可用 workspace、stale default、已登记但宿主不可读、registry 损坏时的结构化恢复动作。现有 Home 至少会列出失效 alias 并给出 `workspace list/add/remove` 的具体下一步（[`src/dyro/home.py:677-693`](../../../src/dyro/home.py)）；Bridge 的单个 `WORKSPACE_NOT_FOUND` 会使 Agent 难以区分“未登记”“路径失效”“本地 Profile 损坏”和“宿主无读取权限”。

**Required fix：** 把现有 resolver 作为唯一实现，并在结果中返回 `resolution_source=explicit|local|default|unique`，不写 recent state。为 `LOCAL_PROFILE_INVALID`、`REGISTRY_INVALID`、`REGISTERED_ROOT_STALE`、`HOST_READ_PERMISSION_REQUIRED`、`AMBIGUOUS_WORKSPACE` 分别定义不带 shell 字符串的 `next_actions`。验收必须覆盖 local Profile 优先、malformed local 不回落、stale default、唯一可用回落、零/多候选非 TTY，以及 registry 在沙箱外但不可读的部分失败。

#### P1-3 — 现有及拟议验收会漏掉真实编码智能体沙箱失败

本轮直接在当前受限 Codex workspace 中运行正常的 `dyro dispatch doctor`，复现 `PermissionError: ... ~/.dyro/local-agent-dispatch/edit-worktrees`：`doctor` 在非 dry-run 下调用创建整棵状态目录的 `dispatch_home`（[`experiments/local_agent_dispatch/cli.py:255-271`](../../../experiments/local_agent_dispatch/cli.py)、[`experiments/local_agent_dispatch/paths.py:36-58`](../../../experiments/local_agent_dispatch/paths.py)）。现有“零写”测试只覆盖 `--dry-run` 且 mock 掉 backend probe（[`tests/test_adversarial_remediation_dispatch.py:2497-2537`](../../../tests/test_adversarial_remediation_dispatch.py)）；wheel CI 又把 dispatch home 指到可写临时目录（[`.github/workflows/ci.yml:73-90`](../../../.github/workflows/ci.yml)），两者都绕开了用户最初遇到的失败。仅比较 workspace 和 registry 文件哈希也看不到临时目录、进程、网络、keyring 或其他用户目录的副作用。

**Required fix：** 新增安装后、非 dry-run 的 R0 黑盒门禁：只允许读的 HOME/XDG/DYRO_HOME、无网络、不可写 workspace、进程 spawn 记录器与全临时目录审计；对每个 R0 请求断言零 write/open-for-write、零网络、零非 allowlist 子进程、stdout 单一 JSON、stderr 无 traceback/ANSI。再在真实 Codex workspace-write 环境跑“registry/工作区均在 sandbox 内”和“registry 可读但工作区在 sandbox 外”两套；Claude/Cursor 的等价试验均 **须人工核**。只有 source-tree mock 或把状态根改到 `/tmp` 不计通过。

#### P2-1 — Skill 和工具面过宽，违背渐进披露并放大上下文成本

Skill 流程要求先跑 doctor/capabilities，而 capabilities 示例携带每个操作的完整输入/输出 schema；同时 MCP 首版列出十多个独立工具，Operation Registry 又覆盖 R0–R3。对普通“为什么 TASK-42 被阻塞”请求，这会把大量无关 schema 注入上下文，并提高误选 `dispatch`、gate execution 或未来 apply 的概率。当前 `skill-render --write` 的真实默认目标是 Dispatch 私有状态树 `.../skills/SKILL.md`（[`experiments/local_agent_dispatch/skill_render.py:164-174`](../../../experiments/local_agent_dispatch/skill_render.py)、[`experiments/local_agent_dispatch/paths.py:101-102`](../../../experiments/local_agent_dispatch/paths.py)），CLI 帮助也只承诺“dispatch home or given path”（[`experiments/local_agent_dispatch/cli.py:374-382`](../../../experiments/local_agent_dispatch/cli.py)），并未证明宿主会发现它。

**Required fix：** 首切片只保留 `hello/capabilities --compact`、`workspace.resolve/list/status`、`task.list/explain/graph` 和 Objective 的既有纯 plan。compact 输出只含版本、operation ID、risk 和 availability；按选中的单一 operation 再取 schema，并以 `schema version + capabilities digest` 缓存。为 SKILL.md、tool catalog 和一次典型 R0 会话设可测 token/byte 上限。触发描述必须正向限定“操作 Dyro 控制面”，并负向排除“委派第二意见/多 Agent panel”（属于 dispatch）。安装必须显式写入宿主真实 discovery 目录，先 preview，处理同名冲突，并可恢复卸载。

### Open Decisions

1. **Public interface：** v1 对 Agent 暴露小规模 typed R0 tools；内部 transport 可以保留 allowlisted `operation` dispatch，但不提供 arbitrary command，也不把完整 registry 一次性变成工具目录。
2. **Distribution：** Core Bridge CLI 留在 `dyro` wheel；Plugin/MCP 推迟并采用单独版本化 integration artifact。若最终仍放 optional extra，必须同样完成 host 资源打包和双向版本握手，不能只增加 Python 依赖。
3. **Mutation：** v1 仅 inspect-and-plan。R1 apply 直到可信宿主授权与真实沙箱证据完成后逐项开放。
4. **Host scope：** 首个承诺应是 Codex 已验证；Claude/Cursor/OpenCode 等只列为 planned，不把“本机能启动 CLI”写成“已支持 Bridge Plugin”。

### Required Fixes / Release Gates

- [ ] 按源码调用图重新分级全部 operation；关闭 `task.gates` R0 缺陷。
- [ ] 删除 v1 MCP apply，文档、Skill、capabilities 和测试四处一致声明 inspect-and-plan only。
- [ ] 固化并复用现有 workspace resolver，补齐来源、部分失败和 actionable recovery schema。
- [ ] 定义 host integration 制品、安装/升级/卸载/回滚和双向版本握手；wheel/sdist 外部安装验收能发现资源漏包。
- [ ] 完成真实 Codex deny-write/no-network 黑盒验收；其他宿主未实测时公开标注 unsupported/experimental。
- [ ] 用 compact capability + operation-on-demand schema 控制 Skill 触发和上下文预算，并做上述十个用户旅程的全新会话前向测试。

### Go / No-Go

| 范围 | 裁定 | 放行条件 |
| --- | --- | --- |
| Phase 0：Bridge JSON envelope + compact capabilities + resolver + 纯 R0 | **Conditional Go** | P0-1 修正；真实 deny-write sandbox 零副作用；installed wheel 通过 |
| `dyro-control-plane` Skill beta | **No-Go** | 真实 discovery 目录、preview/install/uninstall、触发冲突与上下文预算验收完成 |
| Codex Plugin + read-only MCP | **No-Go** | 独立制品、版本握手、Core/Plugin skew、真实 MCP 进程权限 **须人工核**并通过 |
| 任意 R1/R2/R3 MCP apply | **No-Go** | 不属于 v1；可信用户授权 broker 和逐宿主隔离证据完成后另行评审 |
| 对外发布“跨宿主 Dyro Agent Bridge v1” | **No-Go** | 至少一个宿主全链路可安装、可升级、可回滚且制品外验收通过；其余宿主准确降级声明 |

---

# Final Arbitration

## 主审结论

- Arbiter: Codex Root
- Arbitrated at: 2026-08-06
- Locked substrate: `feat/dev@d00d5ca6f1f64edc606ca23d44018033f76f67f4`
- Overall verdict: **原始 Dyro Agent Bridge v1 方案 No-Go；收缩后的 inspect-and-plan-only Phase 0 Conditional Go。**
- Severity after deduplication: **P0 × 4、P1 × 5、P2 × 1**。
- Source changes reviewed: none. This board is a design decision artifact, not an implementation approval.

三名审查者从架构、权限安全、产品与宿主集成三个方向独立反证，核心结论高度一致：Dyro 确实需要给编码智能体提供稳定、结构化、可发现的入站接口，但当前设计把“计划完整性”“用户授权”“操作幂等”“宿主沙箱”四种不同能力混在了一个通用 `apply` 模型中。现有源码只证明个别领域具备其中一部分能力，不能推出通用 Bridge 已具备安全执行条件。

因此本次裁定不是取消 Bridge，而是将第一版产品承诺改为：

```text
编码智能体
  -> 小型 typed tools / Skill
  -> 只读 MCP 或 schema-validated JSON transport
  -> Exposure Catalog（仅描述暴露面）
  -> Core Observation / Plan services（唯一语义与策略来源）
  -X-> 不向 Agent 暴露 apply
```

## 证据权重与独立复核

主审按“当前源码与可复现实验 > 已有设计文档 > 拟议方案”的顺序裁决。三端重复发现已合并，不按票数重复计级：

1. 架构端运行了 74 项聚焦测试，全部通过；这证明现有 Objective、workspace 与 hub 的已实现行为，没有证明通用 Bridge apply。
2. 产品端在隔离 `DYRO_HOME` 下验证了任意目录 resolver 的正向路径，也在真实受限 Codex 环境复现了普通 `dyro dispatch doctor` 尝试创建 `~/.dyro/...` 而失败。该事实说明 source-tree mock、dry-run 或把状态根指向 `/tmp` 不能替代真实宿主验收。
3. 主审独立复核确认：
   - `task gates` 直接调用 `run_gates`，后者启动 argv、写 gate log 并追加 ledger；原 R0 分类被源码直接否定。
   - Objective 的 SHA 绑定了计划语义，但机器调用者能够读取并原样回传；它是完整性摘要，不是不可伪造的人类批准。
   - `objective list/status` 当前使用默认 `recover=True` 的读取路径，存在拿锁和恢复 pending transaction 的可能，不能直接包装为零写 R0。
   - `create_line` 有 preflight/plan，但其多仓 Git 副作用、manifest 落盘和 best-effort rollback 没有构成通用事务与崩溃恢复边界。
   - hub registry 的更新已有独占锁和原子替换，是未来研究收敛式 R1 的较好候选；这仍不等于当前已批准开放。

## 合并后的 P0 阻断项

### P0-F1 — Confirmation digest 不是 Agent 不可伪造的授权

**合并来源：** Architecture P0-3/P0-4、Security P0-CURIE-01、Product P0-2。

`confirmation_sha256` 可用于发现计划过期或语义漂移，但同一 Agent 可以先取 plan 再回填 SHA。`--yes`、`actor`、聊天文本或 Skill 指令同样由调用者可控，不能证明用户在计划生成后作过独立批准。

**裁定：** 删除 v1 的 `dyro_apply_confirmed_plan` 及所有 R1/R2/R3 MCP/Skill apply 能力。未来只有在真实宿主证明存在模型不可见、可验证、短时、单次消费的 approval capability 后，才可逐 operation 重新评审。该能力至少绑定 operation、canonical input、plan digest、workspace identity、effects、session、expiry 与 nonce。无法证明时，R1 永久保留在人类独立 CLI/控制面，不进入 Agent MCP。

### P0-F2 — Operation 风险清单与当前源码不符，必须 deny-by-default

**合并来源：** Architecture P0-2、Security P0-CURIE-02、Product P0-1。

原方案把 `task.gates` 当作 R0，但当前实现会执行通用 argv 并产生持久化记录；`task.answer` 在 local execution Profile 下还可能创建 attempt/worktree、启动 Agent、运行 gates 并改变质量状态。风险不能靠命令名或理想化的新 handler 推断。

**裁定：** 每个 exposure 必须先提交源码调用图、最大风险、上下文有效风险与负向副作用测试，再进入 allowlist。立即做以下拆分：

- `task.gate_definitions.get` / `task.gates.last_result`：只有新建的纯读取实现通过零副作用门禁后才可列 R0。
- `task.gates.run`：至少 R2，v1 不暴露给 Agent。
- `task.answer`：最大 R2；拆成外部答案记录与本地执行等 typed operation 后仍不进入 v1 apply。
- `objective list/status`：不能复用 recovery-enabled reader；必须使用明确 `recover=False` 的 Observation API。
- 未完成调用图审计的 operation：默认不可用，而不是默认 R0。

### P0-F3 — Operation Registry 不能成为第二策略/授权控制面

**合并来源：** Architecture P0-1，并由当前 CLI/Core 分散实现佐证。

当前 line/task/objective 的确认、锁、状态机和变更规则分布在 CLI 与各领域模块中。若新 Registry 同时拥有 risk、policy 与 handler，它会和人类 CLI 分别决定同一动作能否执行，形成策略漂移。

**裁定：** 将 Operation Registry 政名并收缩为 **Exposure Catalog**。它只保存 operation ID、input/output schema、最大风险、协议版本、availability 和对应 Core service 引用；不得拥有授权、业务策略或事务规则。Bridge 禁止调用 `cmd_*`。必须先抽取 transport-neutral 的 Core Observation/Plan service，再由人类 CLI、JSON 与 MCP 共同调用。尚未完成 Core 化的命令不进入 Bridge。

### P0-F4 — “不暴露 R3 MCP 工具”不是同 UID Agent 的安全隔离

**来源：** Security P0-CURIE-03。

当前获得 shell 与工作区写权限的编码智能体仍可绕过 MCP，直接调用普通 `dyro task signoff/merge --yes` 等 CLI。Skill 和工具目录只减少误用面，不能限制同一 OS 身份的调用者。

**裁定：** ADR 必须明确威胁模型：Phase 0 提供的是防误操作和稳定 API，不承诺隔离一个已获得同 UID shell 权限的恶意 Agent。任何“Agent 不能 signoff/merge/push”的强保证，必须由所有入口共同经过的 broker/daemon、外部签名或宿主不可见 capability 实现；仅隐藏 MCP tool 不计安全控制。此项阻断原方案的安全宣称，但在 Phase 0 完全不含 mutation 且文档不作该宣称后，不阻断只读实现。

## 合并后的 P1 必修项

### P1-F1 — 建立真正的零写 Observation 边界

R0 必须满足：零业务写入、零目录/lock 创建、零 ledger/mtime 改变、零网络、零非 allowlist subprocess。它不得触发 recovery、repair、recent state、update check 或隐式缓存。Git 观察使用 `GIT_OPTIONAL_LOCKS=0` 或等价显式契约；支持平台是否仍会改 index 元数据必须实测，不能假设。

测试必须包含 pending Objective transaction、只读 HOME/XDG/DYRO_HOME、不可写 workspace、全临时目录审计、process/network trap，以及 installed wheel 外部黑盒运行。stdout 必须恰为一个 JSON object，stderr 不得出现 traceback 或 ANSI。

### P1-F2 — 计划摘要必须由 operation-specific read set 定义

即使 Phase 0 不 apply，计划模型也要为未来兼容性冻结正确边界。共享 envelope 可以统一，但 read set 不能“一套字段覆盖所有命令”。每个计划至少绑定：

- `protocol_major`
- `operation_id` 与 `operation_schema_version`
- `planner_revision`
- canonical workspace identity 与 config digest
- normalized input
- operation-specific `read_set`，包括解析后的 ref full OID、关键路径/资源身份和所有安全谓词
- semantic effects、warnings、risk 与 expiry

未来 apply 必须在领域权威锁内重算并比较；plan 阶段输出不得被描述为“已经授权”或“可自动执行”。

### P1-F3 — Mutation 不能依赖通用 request ledger

`request_id` 只能做 correlation。多副作用 operation 需要各自的 conflict key、锁顺序、linearization point、durable intent/start/receipt、fencing、`uncertain` 状态和恢复协议。Security 提出的 fd-relative/no-follow 路径方案对未来写侧有价值，但它不阻断纯读取 Phase 0；其 Windows 等价能力仍为 **须人工核**。

若后续发起 R1 试点，候选只考虑具有锁、atomic replace、可从权威状态判断收敛结果的 `workspace.add`。`line.create`、`task.create`、`task.answer` 和 Objective 执行不得作为首个试点。

### P1-F4 — 固化 transport、制品与版本握手

解决 Architecture 与 Product 关于打包方式的分歧如下：

- Core Bridge、JSON transport 和 `dyro-mcp` server code 随同一个 `dyro` distribution 发布；MCP 依赖可使用 optional extra。
- 必须安装真实 `dyro-bridge` / `dyro-mcp` console entry point，Plugin 不调用 ambient `python -m ...`。
- 宿主专属 Plugin/manifest/Skill 安装包是**单独版本化的 integration artifact**，声明兼容 Core/protocol/schema 范围，并拥有文件 ownership manifest、preview/install/status/update/uninstall、原子替换与回滚。
- 握手必须包含 client/integration version、protocol major/minor、operation schema range、planner revision 和 capabilities digest；major mismatch、未知 operation、缺依赖一律 fail closed。
- CI 从 wheel 与 sdist 外部安装，覆盖 N/N-1、Core-newer、Plugin-newer、无 `[mcp]` 四种组合。

### P1-F5 — 复用唯一 workspace resolver，并提供结构化恢复路径

保留现有解析优先级：explicit alias → 向上发现 local Profile → registry default → 唯一可用 workspace。malformed local Profile 必须 fail closed，不得静默回落到别的 workspace。响应增加 `resolution_source`，并区分 `LOCAL_PROFILE_INVALID`、`REGISTRY_INVALID`、`REGISTERED_ROOT_STALE`、`HOST_READ_PERMISSION_REQUIRED`、`AMBIGUOUS_WORKSPACE`，每种返回结构化 `next_actions`，不夹带可直接执行的 shell 字符串，也不写 recent state。

## P2 改进项

### P2-F1 — 收缩 Skill 触发面与上下文预算

首版只暴露 compact capabilities；完整 schema 按选中的单一 operation 获取并以 schema version + capabilities digest 缓存。为 SKILL.md、tool catalog、错误详情与典型 R0 会话设置 token/byte 上限。触发描述要正向限定“读取/规划 Dyro 控制面”，并明确排除 `dispatch` 的第二意见/多 Agent 编排语义，避免 Agent 选择错误入口。

## Open Decisions 最终裁定

1. **Generic invoke vs typed tools：** CLI transport 可保留 schema-validated、allowlisted generic JSON `inspect/plan`；MCP 只提供少量 typed R0/plan tools。禁止 generic shell、arbitrary command 与 generic apply。
2. **MCP packaging：** MCP server code 与 Core 同 `dyro` distribution、使用独立 console entry point；Codex 等宿主 Plugin 是独立版本化 integration artifact。这样既避免第二套 Core 语义，又能独立管理宿主兼容性。
3. **R1 in v1：** 无。v1/Phase 0 仅 inspect-and-plan。`workspace.add` 只能在新的 ADR、实现证据和独立对抗复核后成为后续单 operation pilot。
4. **首个宿主：** 只承诺真实验收通过的 Codex。Claude/Cursor/OpenCode 在各自的 sandbox、approval、安装与进程边界未经端到端验证前标为 planned/experimental，不得宣传为已支持。

## 修订后的模块 Go / No-Go

| 模块 | 当前裁定 | 放行条件 |
| --- | --- | --- |
| 修订 ADR、Exposure Catalog、威胁模型与 operation inventory | **Go** | 仅设计/测试基线，不实现 mutation |
| Phase 0：JSON envelope、compact capabilities、resolver、纯 R0、不可执行 plan | **Conditional Go** | P0-F2/P0-F3 落地；零写与 installed-wheel 黑盒门禁通过 |
| `dyro-control-plane` Skill beta | **No-Go** | Phase 0 通过；真实 discovery、preview/install/uninstall、触发冲突和上下文预算通过 |
| Codex Plugin + typed read-only MCP | **No-Go** | 制品/版本握手/进程权限/真实 Codex sandbox 全链路通过 |
| 任意 R1/R2/R3 Agent apply | **No-Go** | 不属于 v1；可信授权、事务、路径与审计边界逐 operation 另行评审 |
| 跨宿主公开发布 | **No-Go** | 每个宣称支持的宿主独立安装、升级、回滚、权限与沙箱验收通过 |

## 修订后的实施顺序

### Stage A — 先修设计，不写业务功能

1. 新建/修订 ADR-0006：冻结 inspect-and-plan-only、Exposure Catalog、同 UID threat model、非授权 digest 语义和无通用 apply。
2. 产出 operation inventory：逐项记录 source call graph、reads、writes、subprocess/network、locks、recovery、maximum/effective risk、availability。
3. 冻结 JSON envelope、error taxonomy、version handshake、compact capability 和 operation-on-demand schema。

### Stage B — Core Observation / Plan

1. 抽取 transport-neutral Observation services，所有读取显式禁止 recovery/repair/update/recent writes。
2. 抽取 typed Plan services；为每个 operation 定义自己的 `read_set` 和 `planner_revision`。
3. Bridge 只引用这些 services；不得 import/调用 CLI `cmd_*`。

### Stage C — 机器 transport 与真实门禁

1. 增加 dedicated `dyro-bridge` 入口；parse、route、error 全链路只输出一个 JSON object。
2. 建立 deny-write/no-network/no-spawn harness，并在 source tree、wheel、sdist 和真实 Codex workspace-write 环境运行。
3. 加入 malformed local、stale registry、partial permission、pending recovery、Git optional-lock、输出截断与 secret redaction 用例。

### Stage D — Skill，再到 Plugin/MCP

1. 先发布最小 Skill beta，只调用 Phase 0，并验证宿主实际 discovery、误触发和上下文预算。
2. 再提供 typed read-only MCP 与 Codex integration artifact，完成版本偏移、安装/升级/卸载/回滚验证。
3. 不在这一阶段加入 apply。

### Stage E — 单独评审首个 R1 pilot

仅当真实宿主批准能力已经证明后，为 `workspace.add` 单独建 ADR、威胁模型、并发/崩溃/路径故障注入测试和新的对抗评审。该评审不得借 Phase 0 的 Go 结论自动放行。

## Phase 0 验收标准

- Agent 暴露面中不存在 apply、shell、signoff、merge、push、release、publish 或 cleanup。
- `task.gates` 不存在于 R0；纯读取 gate API 触发 subprocess/log/ledger 即测试失败。
- 全部 R0 在只读 HOME/DYRO_HOME/workspace 下零新增路径、零 persistent write、零网络、零非 allowlist subprocess。
- Objective Observation 即使存在 pending transaction 也不恢复、不拿 mutation lock、不改文件。
- malformed local Profile 不回落到 registry；stale/ambiguous/permission errors 给出稳定结构化 code 与 next actions。
- stdout 在成功、schema error、routing error、Core error 下都恰为一个有界 JSON object；无 ANSI、traceback、secret、原始 argv 或未截断 stdout/stderr。
- 从 wheel 和 sdist 安装到 checkout 外仍能运行；缺 optional MCP dependency 时返回结构化 unavailable，而非 Python traceback。
- protocol major 或 operation schema 不兼容时 fail closed；旧 Plugin 不会因新 Core 增加 operation 而自动扩大工具权限。
- SKILL.md 和 Plugin 不宣称其能够安全隔离同 UID shell Agent，也不宣称未实测宿主已支持。

## 最终发布门槛

在上述 Phase 0 条件全部提供可复现证据前，结论保持 **No-Go**。全部通过后，仅把 Phase 0 改为 **Go**；Skill、Plugin/MCP 与任何 mutation 仍分别保留自己的授权和发布门槛。当前最安全、也最有产品价值的下一步，是先让编码智能体能够可靠地“看懂 Dyro、解释状态、生成不可执行计划”，而不是让它代替用户批准和执行交付动作。

— **Final Arbiter: Codex Root**
