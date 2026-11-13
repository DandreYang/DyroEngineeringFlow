# 开发线生命周期与跨线合并（设计草案 v7）

状态：草案 v7，按第六轮审查修订（只改 §2.6.1 身份重检、§3.4 `unknown` 重新对账与收口手册及对应测试、§9），未评审

目标版本：`0.7.x`（`delivery-physics.md:3-4`）

依据：`workspace.py`、`events.py`、`families.py`、`state.py`、`tasks.py`、`delivery-physics.md`；决定：`line-lifecycle-decisions.md`（①–⑥、N4/N6，以及「调整记录」：retire 保留锁文件、doctor 忽略墓碑线锁文件）；审查：`line-lifecycle-and-cross-line-merge.review-v5.md`（本版只闭合：merge `--recover` 回到决定①报告式、补 `already_included`、删除 `--mark-unknown`、R2 保全／漂移与 `missing_ok` 转移；其余契约相对 v5 不改）。

---

## 0. 现网行为摘要（源码锚点）

| 事实 | 位置 |
|------|------|
| 交付原子是开发线 | `delivery-physics.md:35-36` |
| 未登记线 `get_line` → 错误 | `workspace.py:265-266` |
| 同 ID 创建拒绝仅查现役 manifest | `workspace.py:476-477` |
| `Line.base_for` / `storage_for` | `workspace.py:49-53` |
| manifest 含 repos / bases / storage / parent | `workspace.py:81-99` |
| `anchor-reference`：创建时要求 anchor 正位于 `line.branch`，symlink 指向 anchor | `workspace.py:525-534`、`607-611` |
| `_remove_line_worktree` 用 `worktree remove --force` / 可 `rmtree`（**仅创建回滚**） | `workspace.py:412-434`、`437-441` |
| 合并锁路径与超时 | `workspace.py:636-638`、`27`、`909-912` |
| 现 merge **仅**持目标线 `merge.lock`；预检/提交/回滚依赖进程内 `plans`/`committed_heads` | `workspace.py:909-912`、`840-866`、`989-1016` |
| 预检 `merge --no-ff --no-commit`；dry-run 仍会短暂 merge 再 abort | `workspace.py:949-970` |
| 提交环：仅当存在 `MERGE_HEAD` 才 `commit`；无 `MERGE_HEAD` 的仓不进 `committed_heads` | `workspace.py:989-1000` |
| 成功：对 **plans 中每一仓**（含无需新提交者）写 ledger，然后调用方锁外 `append_event` | `workspace.py:1042-1057`、`1096-1106`、`1136-1145` |
| `_require_push_allowed` / `_require_line_worktrees` | `workspace.py:754-781` |
| 子→父 / 父→子 sync 方向 | `workspace.py:1084-1093`、`1122-1128` |
| `doctor` 只返回诊断；含 `FAIL repository`、观察超时 FAIL | `workspace.py:1427-1428`、`1448-1449`、`28-30`、`1521-1528` |
| 家族视图仅直接子女；图对已删 parent 为空 | `families.py:1-4`、`105-117`、`139-140` |
| inbox 依赖当前登记 family | `families.py:422-441`、`448-456` |
| 任务绑线：`get_line` / `known_line_ids`；`list_tasks` + `status` | `tasks.py:279`、`330-331`、`416-422`、`523-528` |
| overlay ≠ ledger；facts 仅标量；键/值长度与内容过滤；须注册 kind | `events.py:1-9`、`28-39`、`43-46`、`104-105`、`117-143`、`358-359` |
| `exclusive_lock`：路径上 `O_CREAT` 打开后 `flock`；释放时 `_unlock` 再 `close` | `state.py:201-257`、`187-197` |
| 单文件原子落盘（journal 更新） | `state.py:34-55` |

---

## 1. Wave 1 范围

### 1.1 纳入

1. **目标线作用域的 doctor 降级**（§4；保留 N4 的 fail-closed）。
2. **可恢复的 `dyro line retire`**（§2）：逐仓日志、按 phase **只向前**继续（决定③）、分支所有权、墓碑；**保留**线锁文件；**intent/result 间隙对账**与 R2 保全／漂移检查（§2.6.1）。
3. **方向明确的跨线本地 merge**（§3）：`--from`、合回基线；**不**声称跨仓原子（决定①）；merge 终态含 `already_included`；`--recover` **仅报告／对账／补 ledger 条件**（§3.4），**不**跨进程 Git 突变。

### 1.2 延期（Deferred，与 v5 相同，未改）

| 能力 | 延期理由 |
|------|----------|
| per-repo merge session、`--continue`/`--abort` | 跨进程恢复需独立协议；现 `_rollback_line_merges` 仅内存 plan（`workspace.py:840-866`） |
| `line add-repo`、`repo remove`/`move` | 多仓配置迁移与 anchor-reference 语义未闭合 |
| retire `--cascade`、unarchive/adopt/reparent | 身份与子孙策略未决 |
| manifest 持久 `degraded` | 违背定律 II（`delivery-physics.md:77-78`） |
| Console `lineage_edges` | 纯投影可由 events 后做 |
| `post_merge` Profile 钩子 | 授权与 A1 门边界未定义（`delivery-physics.md:8-9`） |
| `line merge record` / 外部 merge 补登 | 审计≠集成动作；单独立案 |

---

## 2. `dyro line retire`

### 2.1 目标与非目标

- 卸 worktree、归档 manifest 为墓碑、写事件；可选删**本线独占**本地分支；**不** `git push`（`workspace.py:754-758`）；**不删除** `.dyro/lines/<LINE>.merge.lock`（决定③ 调整记录）。
- **不**复用 `_remove_line_worktree` 的 `--force`/`rmtree` 路径（`412-434`）。
- **不**承诺跨仓原子；承诺逐仓有序、进程内尽力、中断后可按 phase 查明并**向前**继续（决定①③）。

### 2.2 CLI 与标志

```
dyro line retire <LINE>
  [--dry-run] [--keep-branch] [--force]
  [--integration-ref <REF>]
  [--missing-ok]
  [--recover [<op_id>]]
```

| 标志 | 语义 |
|------|------|
| `--dry-run` | 持锁只读预检 + 打印计划；**不**写 ops、不 Git 突变、不归档（区别于 merge dry-run 的短暂 no-commit，`964-970`） |
| `--keep-branch` | 跳过各仓「删分支」步 |
| `--force` | **仅**放宽：相对 `integration_ref` 非祖先的本地分支，在已写 `refs/dyro/retired/...` 后仍可删；**不**越过 §2.5 所有权限制、不脏树、不用 `worktree remove --force`（决定④） |
| `--integration-ref REF` | 覆盖每仓包含检查目标；未给时：有父 → 父线同仓 `parent.branch`；无父 → `line.base_for(repo)`（**非**静默 `policy.default_base`） |
| `--missing-ok` | 允许跳过「移除已缺失 worktree」；仍须写保全 ref（若 HEAD/分支可解析）并走后续；用于损坏子线先退役（决定⑤ / N5） |
| `--recover [op_id]` | 读 `.dyro/ops/<op_id>.json`（缺省则选该线未终态最新 retire op）；**按 phase 核对现实后只向前继续**；**不**重跑 §2.3 完整预检；**强制使用 journal 内冻结选项**（§2.6），拒绝与之冲突的新标志 |

**未终态拦截（决定①）**：非 `--recover` 的 `retire`/`merge`/`merge-into-base` 若发现该主体已有 `phase` 未 `completed`/`aborted` 的同族 op → **立即停止**，打印 `op_id` 与 `--recover` 用法，不开始新 op、不重做预检突变。

**无** Wave 1 `--abort`、**无** `--mark-unknown`。已卸 worktree 不自动重建，靠 `refs/dyro/retired/<line>/<repo>` 人工恢复。某仓停留 `unknown` 时 op **保持未终态并继续拦截**；报告列出可手动执行的 Git 命令，用户处理后**再跑** `--recover`。

### 2.3 前置（仅正常路径；锁外预检 + 锁内重检）

**适用范围**：全新 retire（无未终态 op）。`--recover` **不适用**本段完整清单，改走 §2.6.1。

1. 无存活他进程持有本线 `merge.lock`（同 `909-912` 获取路径）。
2. 无未退役**直接**子线（`families.py:105-113`）；**不**因「子线损坏」放宽——子线须先 `retire [--missing-ok]`（N5）。
3. 存在状态非 `done` 的任务引用该线 → **拒绝**（决定⑤；扫描 `list_tasks` + `status`，`tasks.py:416-422`、`523-528`；绑线校验 `279`、`330-331`）。
4. 存在 `.dyro/lines/retired/<LINE>.toml` 墓碑且仍有现役 manifest → 不一致，拒绝并提示 `--recover`。
5. 每仓按 `storage_for`：
   - **linked-worktree**：路径存在（或 `--missing-ok`）、在 anchor worktree 清单内、当前分支=`line.branch`（缺失/不可解析 → 拒绝，除非 `--missing-ok` 且仅跳过 remove）、`status --porcelain` 空、无 `MERGE_HEAD`/`CHERRY_PICK_HEAD`/`REVERT_HEAD`/`rebase` 进行中。
   - **anchor-reference**：symlink 指向配置 anchor（对照 `1488-1494`）；**不**要求对 symlink 做独立 porcelain；分支删除规则见 §2.5。
6. **集成包含**：anchor 上解析 `line.branch` 与 `target_ref`（§2.2）；`merge-base --is-ancestor <line-HEAD> <target-ref>`。失败且未 `--force` → 拒绝；`--force` 仅影响后续删分支，**不**跳过 R1 保全。

持 **身份锁 + 线锁 + 物理锁**（§2.4）后重做本段全部检查。

### 2.4 锁序：身份锁、线锁、物理资源锁（决定② / N1；闭合跨线并发）

| 层 | 键 / 路径 | 用途 |
|----|-----------|------|
| **L0 身份锁** | `.dyro/locks/line-identity.lock`（单文件；实现可用 `exclusive_lock`，`state.py:201-257`） | 凡**读或改**父子关系、线身份（create/spawn/retire）、或任务→线引用的入口必须持有；持锁内重检 |
| **L1 线锁** | `merge_lock_path(config, LINE)`（`636-638`） | 该线 merge/sync/retire 外层互斥 |
| **L2 物理资源锁** | `.dyro/locks/anchor/<stable-hash>.lock`；键 = 规范化绝对 **anchor**（`repository_path`，`277-281`） | 凡改 Git/线绑定：create、spawn、merge、sync、retire、task merge；多仓按 **repo_id 升序** |
| **L3 overlay** | `overlay_lock`（`events.py:337-339`） | 仅归档后的 event；用 locked API（`342-359`） |

**加锁顺序（防死锁）**：L0 → L1（涉及的每条线，线 ID 升序）→ L2（repo_id 升序）→ Git 突变 → 释 L2/L1 后视需要 L3。释放逆序。

**L0 覆盖入口**：`line create`、`line spawn`、`line retire`（含 `--recover`）、任务创建/改绑线（写 `task.line`）、以及任何扫描「是否有子线 / 未完成任务引用该线」的检查与写入。纯仓内 `merge`/`sync` 若**不**改 parent/身份/任务绑线，可不持 L0；但 retire 的「无直接子」「无未完成任务」必须在 L0 内完成。

### 2.5 分支所有权（决定④ / N3）

删除 `refs/heads/<line.branch>` **当且仅当**全部为真：非 `--keep-branch`（以 journal 冻结值为准，§2.6）；storage ≠ `anchor-reference`；无其他**现役**线（含 hotfix）引用同分支名；anchor **当前检出不是**该分支；分支 tip == 本仓 R1 已保全 SHA。任一条失败 → 只解除关联，不删 ref。`--force` **不能**越过本段。

### 2.6 步骤、journal、崩溃（无删锁）

**Journal**：`.dyro/ops/<op_id>.json`（单文件原子写，`state.py:34-55`）。每次 Git 突变：**先**写 `intent`（repo、动作、原 HEAD、**钉死的目标 SHA**），**后**写 `result`（ok/error、新 tip）。字段须含完整 manifest 快照、每仓路径、`storage_mode`、`integration_ref`、逐仓 `phase`（`r0`…`r5` / `completed`）、计划写入的 event facts 草稿（§7），以及：

**`effective_options`（冻结，R0 写入后不可改）**：

| 字段 | 来源 |
|------|------|
| `keep_branch` | CLI `--keep-branch` |
| `force` | CLI `--force` |
| `missing_ok` | CLI `--missing-ok` |
| `integration_ref` | CLI 或 §2.2 默认解析结果（已解析字符串） |

`--recover` **必须**使用上述冻结值推进 R3/包含语义；若命令行再传与冻结值**冲突**的同名选项 → **拒绝**，打印 journal 中的值与冲突项。未再传选项则静默沿用冻结值。

| 阶段 | 动作 | 中断后 |
|------|------|--------|
| R0 | 写 op：`kind=retire`、快照、`repos_ordered`、`effective_options`；**先**按 §7 校验 event 载荷 | `--recover` 读取 |
| R1 | 每仓：intent 钉死 `saved_sha` 后 `update-ref refs/dyro/retired/<LINE>/<repo> <saved_sha>`；再写 result | §2.6.1 对账；**禁止**覆盖已确认或冲突 SHA |
| R2 | linked：在 **HEAD==R1 钉死 SHA** 且路径／分支仍匹配 intent 时，清洁树上 `worktree remove`（禁 `--force`）；anchor-reference：仅 `unlink`；见 §2.6.1 漂移与 `missing_ok` | §2.6.1 |
| R3 | 按 §2.5 + 冻结 `keep_branch`/`force` 删分支（或跳过） | tip≠保全 SHA → `skipped_moved` |
| R4 | **全部仓完成后** `atomic` 将 manifest 移至 `.dyro/lines/retired/<LINE>.toml` | 墓碑未写则仍现役；任一仓仍 `unknown` → **不得**进 R4 |
| R5 | `append_event` kind `retire`（先扩 `EVENT_KINDS`）；失败 → `events.gap`（`66-87`）；op → `completed` | 不回滚 Git；**不**删 `.merge.lock` |

**锁文件（决定③ 调整）**：retire **永不**删除线锁文件。互斥仍靠打开 fd 上的 `flock`（`state.py:201-257`），不靠 unlink。墓碑线的残留锁文件由 doctor **忽略**（§4）。

**顺序**：分支删除在归档/事件**之前**（每仓 R3）；事件在墓碑之后。

### 2.6.1 按 phase 的 `--recover` 与 intent/result 对账（闭合 review-v5 R2）

**入口**：`dyro line retire --recover [op_id]`。**禁止**调用 §2.3 全量预检。真源：`.dyro/ops/<op_id>.json`。若 R4 已成功或现役 `_state_path` 已不存在：线元数据取归档 `.dyro/lines/retired/<LINE>.toml` + journal 快照，**不得**依赖 `get_line`（`265-266`）。锁：L0 → 该线 L1 → `repos_ordered` 的 L2（升序）。选项：仅用 `effective_options`。

**身份不变量重检（闭合 review-v6 高项）**：上次进程退出即释放 L0，期间他进程可能给仍现役的本线新增子线或未完成任务。故 `--recover` 在**取得 L0 之后、任何突变之前**，必须重做 §2.3 第 2、3 项（无未退役直接子线；无状态非 `done` 的任务引用本线，`families.py:105-113`、`tasks.py:416-422`、`523-528`），并在整个恢复期间持有 L0。任一不成立 → **停止**，报告阻断的子线／任务；用户先退役子线或完成任务后再 `--recover`。只跳过 §2.3 的 Git 类预检（第 5、6 项），身份类检查不跳过。

**间隙原则**：Git/`os` 动作可能已成功而 `result` 未落盘（`atomic_write` 在动作之后，`state.py:34-55` 不能消除该间隙）。恢复时**先对账再写 result**；**绝不**在未核对 intent 钉死 SHA 时重放 `update-ref`，**绝不**覆盖已存在但与 intent 不符的 ref。

**R2 移除前必检（闭合 review-v5 高项）**：在执行 `worktree remove` / `unlink` 之前，必须同时成立：（1）worktree **HEAD == R1 钉死的 `saved_sha` S**；（2）路径仍为 journal intent 中的路径；（3）分支（或 symlink 目标）仍匹配 intent。任一漂移（例如 R1 后用户在 worktree 上 detached 并提交新 SHA）→ **停止**，打印漂移报告（期望 S、实际 HEAD、路径、分支），**禁止**仅凭「清洁」删除（不得走 `412-434` 的 `--force`/`rmtree`）。

#### 全状态表（retire）

记号：`I`＝已写 intent；`R`＝已写 result；`S`＝intent/`saved_sha` 钉死的 SHA；路径＝worktree 或 symlink；支＝`refs/heads/<line.branch>`。

| 步 | journal | 现实 | 动作 |
|----|---------|------|------|
| R1 | 无 I | 保全 ref 不存在；HEAD 可解析为 H | 写 I（`saved_sha=H`）→ `update-ref` → 写 R |
| R1 | 无 I | 保全 ref 不存在；HEAD 不可解析 | 若冻结 `missing_ok` → R=`skipped_no_head`；否则 **停止** |
| R1 | 无 I | 保全 ref 已存在 | **停止 unknown**（无钉死 SHA，禁止发明） |
| R1 | I=`S`，无 R | ref 不存在 | 以 **S** 执行 `update-ref`（不读可变 HEAD）→ 写 R=`ok,S` |
| R1 | I=`S`，无 R | ref 存在且 `==S` | **确认**：只写 R=`ok,S`；**禁止**再 `update-ref` |
| R1 | I=`S`，无 R | ref 存在且 `≠S` | **停止 unknown**；**永不覆盖** |
| R1 | R=`ok,S` | ref `==S` | 跳过 |
| R1 | R=`ok,S` | ref `≠S` | **停止**冲突 |
| R1 | R=`ok,S` | ref 缺失 | **停止**（保全丢失） |
| R1 | R=`skipped_no_head` | — | 跳过 |
| R2 | 无 I；路径仍在 | HEAD==S、路径／分支匹配、清洁 | 写 I → remove/unlink → 写 R |
| R2 | 无 I；路径仍在 | HEAD≠S **或** 路径／分支不匹配 **或** 脏 | **停止**；漂移报告；**禁止** remove |
| R2 | 无 I；路径**已无**；冻结 `missing_ok=true` | — | 写 I + R=`skipped_missing`；**继续** |
| R2 | 无 I；路径**已无**；冻结 `missing_ok=false` | — | **停止**（未获准的缺失） |
| R2 | I，无 R | 路径仍在；HEAD==S、身份匹配、清洁 | remove/unlink → 写 R |
| R2 | I，无 R | 路径仍在；HEAD≠S 或身份漂移或脏 | **停止**；漂移报告 |
| R2 | I，无 R | 路径**已无** | **确认**：写 R=`ok` 或沿用 `skipped_missing`；**禁止**重放 remove |
| R2 | R 完成/`skipped_missing` | 路径已无 | 跳过 |
| R2 | R 完成 | 路径仍在 | **停止 unknown** |
| R3 | 冻结 `keep_branch=true` | — | 记 `skipped_keep`；不删 |
| R3 | 无 I / I 无 R | 支仍在、tip==S、§2.5 满足 | 写 I → 删支 → 写 R |
| R3 | I，无 R | 支**已不存在** | **确认**：写 R=`ok`/`skipped_absent` |
| R3 | I，无 R | 支在但 tip≠S 或所有权失败 | 写 R=`skipped_moved`/`skipped_unowned`；不删 |
| R3 | R 完成/skipped | 与声称一致 | 跳过 |
| R4 | 无 I / I 无 R | 现役 manifest 仍在；**全部仓**已非 `unknown` | 写 I → 归档 → 写 R |
| R4 | I，无 R | 现役已无且墓碑存在且内容与快照一致 | **确认**：写 R=`ok` |
| R4 | I，无 R | 现役与墓碑并存或内容不符 | **停止 unknown** |
| R4 | R 完成 | 仅墓碑 | 进 R5 |
| R5 | 无 I / I 无 R | 无同 `op_id` 的 retire 事件 | 写 I → `append_event` → 写 R；失败 → `events.gap` |
| R5 | I，无 R | 已有同 `op_id` 事件（facts 含 `op_id`） | **确认**：写 R=`ok`（去重）；不二次 append |
| R5 | R 完成 | — | op → `completed` |

打印每仓 `done` / `pending` / `unknown`。对 `unknown`：列出手动命令（例如：使 worktree HEAD 回到 S、中止未完成 merge、或确认路径状态），**含破坏性标注**；**不**提供 `--mark-unknown`。Wave 1 **不**自动重建 worktree、**不**回滚已删分支。

### 2.7 墓碑与家族历史（决定⑤）

- create/spawn：除现役 `_state_path`（`476-477`）外，若存在 `retired/<id>.toml` → **拒绝**，ID 永不复用（在 L0 内检查）。
- `line inbox --line <退役线>`：从墓碑读 `parent`/id，打开 `.dyro/families/<family>/` 只读；`line post` 对退役线拒绝。
- 已 `done` 任务/Objective 可经墓碑解析线元数据。

### 2.8 测试要点

- 脏树 / detached / 缺分支 → 拒绝；`--missing-ok` 仅跳过 remove。
- 共享分支 / anchor-reference / tip 漂移 → 不删分支。
- 子线缺失 worktree：子 `--missing-ok` 可退役，父仍须无存活直接子。
- dry-run 零突变；`--recover` 在 R1/R2 动作成功但 result 未写时能确认；已确认 `saved_sha` 与冲突 SHA 均不被覆盖；R4 后 recover 读归档而非 `get_line`。
- R1 后 worktree 上新 detached 提交且清洁 → `--recover` **停止并报告漂移**，不 remove。
- 无 R2 intent、路径已无、冻结 `missing_ok=true` → 写 `skipped_missing` 并继续；`missing_ok=false` → 停止。
- 原命令 `--keep-branch` 后仅 `--recover` 不得删支；冲突新选项 → 拒绝。
- 无 `--recover` 遇未终态 op → 停止；retire 后锁文件仍在，doctor 对墓碑线不报该锁为残留。
- 某仓 `unknown` 时不得进 R4；无 `--mark-unknown`；事件 facts 合规且在 Git 前预校验。
- R1 后进程退出，他进程给本线 spawn 子线或绑定未完成任务 → `--recover` 在突变前停止并报告，不进入 R2–R4。

---

## 3. 跨线本地 merge

### 3.1 语义

复用 target worktree 收 merge（`926-1057`）：

- 保留：`dyro line merge <CHILD> --into <PARENT>`（`1073-1078`）。
- 跨线：`dyro line merge <TARGET> --from <SOURCE>` — 不改 `parent`。
- 合回基线：`dyro line merge-into-base <LINE> [--base-ref REF] [--repo REPO]…`  
  - **target** = anchor 上当前检出的基线分支写入点；**source** = 线 HEAD。  
  - 默认 ref：`line.base_for(repo)`；可选 `--policy-default` → `policy.default_base`。  
  - 锁内验证（决定②）：anchor 干净、**当前分支即目标基线**、无进行中操作；不满足 → 拒绝，**不**自动 checkout。  
  - 不调用 `get_line("main")`（`265-266`）。

仓库集：`sorted(set(target.repositories) & set(source.repositories))`；空 → 拒绝。

### 3.2 部分集成语义（决定⑥）

| 条件 | 结果 |
|------|------|
| 源的每个仓都在目标中且全部合入（含 `already_included`） | 成功「已合入」，退出 0 |
| 源有目标没有的仓 | 结果 **`partial`**，列出排除仓；默认退出非 0；`--allow-partial` → 退出 0，输出仍标 `partial` |
| 交集空 | 拒绝 |

ledger/event **只**记录实际合入仓（有界编码，§7），不写「整线已合入」（定律 I，`delivery-physics.md:66-73`）。现网对 plans **每一仓**写 ledger（`1042-1057`），含无需新提交者；Wave 1 以 `already_included` 显式表达该成功。

### 3.3 前置

- `_require_push_allowed`；合入方 `_require_line_worktrees(..., clean=True)`；源读 HEAD（`1079-1080`）。
- 子线 doctor 阻断仅 direct-child 兼容路径（`1081-1083`）；兄弟 `--from` 不跑全树。
- Wave 1：对 `anchor-reference` 目标 **拒绝** `git merge`。
- 未终态同主体 merge op → 拦截并指向 `--recover`（§2.2）。

### 3.4 锁、journal、崩溃与恢复（决定①②；闭合 review-v5）

锁序：目标（及需要时源）L1 → 涉及仓 L2（升序）→ Git → 释后 L3 写 event。不改父子则可不持 L0。

**`effective_options`（M0 冻结）**：`allow_partial`、以及本 op 实际采用的 `integration`/`base-ref` 解析结果（若适用）。`--recover` 沿用冻结值；冲突新选项 → 拒绝。

| 阶段 | 动作 | 中断后 |
|------|------|--------|
| M0 | **强制**写 op（target/source/plans/`original_head`/`source_head`/repos/`effective_options`）；**先** §7 校验 | `--recover` **只读对账** |
| M1 | 选定交集预检 no-commit；失败 abort probes（`969-987`） | 同现网；进程内 |
| M2 | 逐仓：有 `MERGE_HEAD` 则 commit（`989-1000`）；源已是祖先、无 `MERGE_HEAD` → 记 `already_included`（HEAD 仍为 `original_head`）；失败则 `_rollback_line_merges`（`840-866`、`989-1016`） | 见下表；**释锁前**须把每仓终态写入 journal |
| M3 | 对 journal 终态为 `committed` **或** `already_included` 的仓写 ledger（含 `op_id`）；缺则补登，**不**重放 Git；幂等键 `(op_id, repo)` **且**核对 journal 终态。补登 `already_included` **仅当** `git merge-base --is-ancestor <source_sha> HEAD` 成立 | 与现网「每仓都 ledger」（`1042-1057`）对齐 |
| M4 | `append_event`：`merge` 或 `merge_cross`；同 `op_id` 已存在则跳过 | `events.gap` |

#### 每仓持久终态（journal `repos.<id>.outcome`）

| 终态 | 含义 | 写入时机 |
|------|------|----------|
| `not_started` | 本仓未提交合并；HEAD 仍为 `original_head`，无本 op 的成功 `result`，且**尚未**核验为已包含 | M0 初始；或对账确认从未提交且非已包含 |
| `committed` | 本仓合并提交成功；`result_head` 已落盘且 HEAD==`result_head` | **仅**在 Git commit 成功**且** `result` 已原子写入后 |
| `already_included` | 预检／提交时无本 op 的 `MERGE_HEAD`；`source_sha` 已是（原）HEAD 祖先，无需新提交；HEAD 保持 `original_head` | 进程内在 M2 确认后；或 `--recover` 用 `merge-base --is-ancestor <source_sha> HEAD` 核验后写入。**计成功**，与 `committed` 一样进 M3 |
| `rolled_back` | 进程内回滚成功到 `original_head`；证据已落盘 | **必须在释放 L1/L2 之前**写入（决定①「可查明」） |
| `unknown` | 无法安全判定；**保持 op 未终态并继续拦截** | 对账无法确认 |

**禁止**：无 `result` 时把 HEAD 某值推断为 `committed`；仅凭 ledger 行判定 `done` 而不读 journal 终态；把「无 `MERGE_HEAD` 且 HEAD==`original_head`」默认写成 `not_started` 而跳过 ledger（与 `1042-1057` 不符）。

#### merge `--recover`：报告式对账（决定① 第 4 点）

`--recover` **永不**执行跨进程 Git 突变：不自动 `commit`、不自动 `merge --abort`、不自动 `reset`。它只：（1）按 Git 事实调和每仓 `outcome` 并 `atomic_write` journal（`state.py:34-55`）；（2）在条件满足时**补登** ledger；（3）打印用户可手动执行的恢复命令（破坏性命令须标注）。正常路径的进程内回滚（`840-866`）不变。

| journal | 现实 | 动作（仅 journal／ledger／报告） |
|---------|------|------|
| 无 result；HEAD==`original_head`；无 `MERGE_HEAD`；`merge-base --is-ancestor <source_sha> HEAD` | 已包含 | 写 `already_included`；可补 ledger；**不**再 commit |
| 无 result；HEAD==`original_head`；无 `MERGE_HEAD`；**非**祖先 | 干净未开始 | 记/保持 `not_started`；op 以 `aborted` 收口，报告「需要合并请重新执行 `dyro line merge`（新 op）」；**不**自动 commit |
| 无 result；存在 `MERGE_HEAD` | — | `unknown`；按下方收口手册第 1 点提示 `git merge --abort`，**不**建议手动完成提交 |
| 无 result；HEAD∉{`original_head`} 且无已写 `result_head` | — | `unknown`；**禁止**补 ledger 成功行；报告回 `original_head` 或确认期望 tip 的手动命令 |
| `committed`+`result_head=H`；HEAD==H；无后续 `rolled_back` | — | 可补 ledger/event；视为已调和 |
| `committed`+`result_head=H`；HEAD==`original_head` | 合并曾成功后被回滚 | **不得**当成功；若无 `rolled_back` 记录 → 写 `rolled_back`（若能核验）或 `unknown` |
| `already_included`；HEAD==`original_head`；祖先核验仍成立 | — | 可补 ledger；已调和 |
| `already_included`；核验失败或 HEAD 漂移 | — | `unknown` |
| ledger 已有 `(op_id,repo)` 指向 H，但 journal 为 `rolled_back` 或 HEAD==`original_head` 且非 `already_included` | 回滚后的陈旧 ledger | **不**视为 done；保留 `rolled_back`/`unknown`；不删历史 ledger 行，只报告「ledger 陈旧」 |
| `rolled_back`；HEAD==`original_head` | — | 已调和 |
| `rolled_back`；HEAD≠`original_head` | — | `unknown` |
| `unknown` | — | `unknown` **不是粘滞状态**：每次 `--recover` 都按 Git 事实**重新进入本表上面各行**判定，可转为 `not_started`／`already_included`／`rolled_back`；仍判定不了 → 保持 `unknown` 并重打手动命令。**永不**由此推断 `committed` |

**`unknown` 收口手册（闭合 review-v6 中项）**：目标是把该仓恢复到可识别的事实，然后重跑 `--recover`。

1. 有 `MERGE_HEAD`：`git -C <repo> merge --abort`。之后 HEAD==`original_head` 且无 `MERGE_HEAD`，重跑后判为 `not_started`（或源已是祖先时判为 `already_included`）。
2. HEAD 不等于 `original_head`：先把要保留的提交另存到分支（`git -C <repo> branch <keep-name> HEAD`），再执行 `git -C <repo> reset --hard <original_head>`。**破坏性**：会丢弃工作区未提交改动。之后重跑，判为 `not_started`。
3. **不要**手动完成合并提交来收口：没有本 op 的 `result`，手动提交只会被判为 `unknown`。正确做法是先按 1 或 2 回到 `original_head`，让本 op 以 `aborted` 收口，再**重新执行**一次 `dyro line merge`（新 `op_id`）。

**进程内回滚（M2，同进程）**：对每个曾进入 `committed_heads` 的仓，回滚成功后**立即** `atomic_write` journal `outcome=rolled_back`（含 `from_head`、`original_head`）；全部回滚结果落盘后才释放锁。回滚失败的仓 → `unknown`。

**op 级终态**：仅当**每一个**计划仓的 `outcome ∈ {not_started（且本 op 不再继续该仓）, committed, already_included, rolled_back}` 均已调和（**全部为已知终态**），才可将 op 标为 `completed`/`aborted`。任一仓仍为 `unknown` → op **保持打开并拦截**同主体命令。**无** `--mark-unknown`。

对外措辞：**逐仓执行，失败时进程内尽力回滚；中断后可查明每仓状态**（决定①）。dry-run：保持短暂 no-commit 语义，CLI 须声明。

### 3.5 测试

- 兄弟 `--from` + ledger/event；`merge-into-base` 方向防回归。
- 源超集 → `partial`；`--allow-partial` 退出码；交集空拒绝。
- A 已包含 source（无 `MERGE_HEAD`）、B 正常 commit：A → `already_included` 且仍有 ledger 行（对照 `1042-1057`）；整体可成功收口。
- A 已 commit、B 失败、A 已回滚后杀进程：journal 有 `rolled_back`；`--recover` **不**补成功 ledger、**不**自动再 commit；打印手动命令。
- 无 result 且 HEAD≠`original_head` → `unknown`，不推断成功；`--recover` 后仍拦截直至人工使状态可识别并再 recover。
- 冻结 `allow_partial` 与冲突新选项；CLI／文档无 `--mark-unknown`。
- M1 后中断留下 `MERGE_HEAD`：首次 `--recover` 写 `unknown`；手动 `merge --abort` 后再 `--recover` → `not_started`，op 以 `aborted` 收口并解除拦截；随后新 op 可正常合并。

---

## 4. doctor 与就绪（N4；锁文件调整）

| 调用点 | v6 |
|--------|-----|
| `existing_line_workspace` / `cmd_start` | 可忽略**他线**的 `FAIL line:/hotfix:`；**仍阻断**：`FAIL external…`、**目标线**前缀 FAIL、工作区根相关 FAIL、目标线所依赖仓的 **`FAIL repository …`**（`1448-1449`）、以及 **`FAIL observation: deadline…`** / `is_observation_deadline_finding`（`28-30`、`1216+`、`1521-1528`） |
| `merge_line` 子线 doctor | **保留**（`1081-1083`） |
| missing-origin | **保留**可跳过（`1537-1552`） |
| 墓碑线锁文件 | 若存在 `.dyro/lines/retired/<id>.toml`，doctor **不**将 `.dyro/lines/<id>.merge.lock` 报为残留/异常 |

过滤在调用方；`doctor` 仍全局扫描。

---

## 5. 横切 mutation 契约

1. 真源顺序：Git HEAD → `ledger.jsonl` → overlay events（`events.py:1-9`；定律 I/II）。
2. 持久 journal：`.dyro/ops/<op_id>.json`；intent→result；幂等键 `op_id`（ledger 再加 `repo`）；**含 `effective_options`**；`--recover` 拒绝冲突选项。
3. 锁：L0 → L1 → L2 →（释后）L3；锁内重检；回滚结果**释锁前**落盘。
4. **Git 突变前**校验 event facts（§7）。
5. 未终态 op：同命令停止并指向 `--recover`；retire recover 按 phase **向前**对账（§2.6.1）；merge recover **仅报告／调和 journal／条件补 ledger**，不跨进程 Git 突变（§3.4）；**无 result 不推断成功**。
6. 不声称跨仓事务；partial / unknown / already_included 如实报告（决定①⑥）。
7. retire **不**删线锁文件；doctor 忽略墓碑线锁文件。
8. op 解除拦截：**仅当**全部计划仓调和为已知终态（非 `unknown`）。**无** `--mark-unknown`；用户按报告手动处理后再 `--recover`。

---

## 6. 决定落地对照（①–⑥ + 锁文件调整）

| # | 要求 | 本文 |
|---|------|------|
| ① | 不跨仓原子；ops journal；未完成则停下；`--recover` **报告并给手动命令**（首批不跨进程自动回滚／自动续 commit） | §2.2、§2.6、§2.6.1、§3.4 |
| ② | 基线路径验证；物理锁；全入口；repo 序；锁内重检；跨线身份互斥 | §2.4、§3.1、§3.4 |
| ③ | 每仓 save→remove→条件删支；全仓后归档；再 event；**保留锁文件**；恢复**只向前** | §2.6、§2.6.1、§4 |
| ④ | 所有权与 anchor-reference；`--force` 边界 | §2.5、§2.2 |
| ⑤ | 墓碑；未完成任务阻断；家族只读；`--missing-ok` | §2.3、§2.7、§2.2、§2.6.1 R2 |
| ⑥ | 超集 → `partial`；`--allow-partial`；空交集拒绝 | §3.2 |

---

## 7. 账本与事件（闭合 80 字符与过滤）

约束（否则 `EVENT_WRITE_INVALID`）：

- kind ∈ `EVENT_KINDS`（扩 `retire`、可选 `merge_cross`；`events.py:28-39`、`358-359`）。
- facts ≤ 16 键；键 ≤ 40 且仅 `[a-z0-9._-]`（`43-46`、`109-114`）。
- 字符串值 ≤ **80**（`45`、`123-124`）；禁控制字符；禁子串 `/` `\` `http:` `https:` `token=` `secret`（`127-131`）。
- `_safe_token`：≤80 与首字符限制（`104-105`）。

| fact | 类型 | 含义 |
|------|------|------|
| `repo_count` | int | 实际合入/涉及仓个数（0…1e6，`120-121`） |
| `op_id` | str | 指向 journal；完整仓序只在 journal |
| `repos_digest` | str | 可选短摘要 ≤80、无 `/` |
| `partial` | bool | 可选 |

**校验时机**：M0/R0 即校验；Git 前失败则整单拒绝。补登 event 用同一 `op_id` 去重。ledger phase：`line_retire`、`line_merge` / `line_sync`、`*_failed`（`974-981`）。跨线 `family`：用 `target.id` 或空。`already_included` 仓仍写 ledger（同 `1042-1057`），不另发明「跳过登账」路径。

---

## 8. 落地顺序

1. L0 + L2 接入要求入口 + 回归。
2. doctor 调用方过滤 + 墓碑锁忽略。
3. ops journal（含 `effective_options`）+ 未终态拦截 + §2.6.1 / §3.4 对账（retire 向前；merge 报告式；**无** `--mark-unknown`）。
4. event 有界 facts 预校验。
5. `merge --from` / `merge-into-base` + partial + `already_included`。
6. `line retire` + 墓碑 + 家族只读（不删锁文件；R2 保全／漂移）。

每 PR 附：对照 §2.6.1 / §3.4 的中断恢复手册（含手动命令与破坏性标注）。

---

## 9. 审查闭合表（相对 review-v5）

| review-v5 项 | v6 状态 | 闭合处 |
|--------------|---------|--------|
| 恢复与完整预检冲突 | **闭合** | §2.6.1；R2 保全／漂移 |
| 正常路径≠日志原子 | 保持闭合 | §3.4；`state.py:34-55` |
| 缺分支／保全／R2 新提交 | **闭合** | §2.6.1：移除前 HEAD==S + 身份匹配；漂移停止 |
| merge 跨进程恢复／无操作成功 | **闭合** | §3.4：`already_included`；`--recover` 报告式；祖先核验后补 ledger（`1042-1057`） |
| Git／ledger／event | **闭合** | §3.4 M3；`already_included` 与每仓 ledger 对齐 |
| retire 多仓／R2 missing | **闭合** | §2.6.1：无 I、路径已无、`missing_ok` 分支 |
| 八项决定／①③ 恢复承诺 | **闭合** | §3.4 回决定①报告式；§2.6.1 决定③只向前 |
| N2／N6 | **闭合** | §2.6.1、§3.4；**删除** `--mark-unknown` |
| 核验范围不足 | 仍部分 | §4；home/cli 接入点仍非本草案可独立验证 |
| v4 已闭项（锁文件／L0／事件长度／物理锁／选项冻结等） | 保持闭合 | §2.4、§2.6、§4、§7 |
| **新高**：R2 仅凭清洁删除 | **闭合** | §2.6.1 R2 |
| **新中**：已包含无成功终态 | **闭合** | §3.4 `already_included` |
| **新中**：R2 `missing_ok` 无 intent | **闭合** | §2.6.1 |
| **新中**：`--mark-unknown` 收口不清 | **闭合** | 全稿删除；unknown 保持拦截 |
| **review-v6 新高**：恢复期间身份不变量未重检 | **闭合（v7）** | §2.6.1 取 L0 后、突变前重检无直接子线／无未完成任务，恢复全程持 L0 |
| **review-v6 新中**：`unknown` 不前进与再次 recover 矛盾 | **闭合（v7）** | §3.4 `unknown` 每次重新判定 + 收口手册；不推断 `committed` |

---

## 10. 源码索引

`Line`：`39-53` · manifest：`81-99` · 创建拒绝：`476-477` · anchor-reference：`525-534`、`607-611` · 危险 remove：`412-434` · 锁路径：`636-638`、`909-912` · merge/rollback／每仓 ledger：`840-866`、`896-1057`（提交环 `989-1000`；ledger 环 `1042-1057`） · doctor：`1427-1528`、`28-30`、`1216-1217` · `state.atomic_write`／`exclusive_lock`：`34-55`、`201-257` · events：`1-45`、`66-87`、`104-143`、`337-415` · families：`1-4`、`105-117`、`139-140`、`422-456` · tasks：`279`、`330-331`、`416-422`、`523-528`。

---

*Wave 1：实现须与决定①–⑥及锁文件调整字面一致；任何「原子」表述仅允许用于单仓单步 Git 结果与单文件 journal 落盘，不得用于跨仓操作或 Git↔journal 间隙。merge `--recover` 不得跨进程执行 Git 突变；retire `--recover` 只向前继续且 R2 必须以 R1 保全 SHA 与身份匹配为前提。*
