# Dyro missing-origin / 诚实性 / 任务关闭 会审

Date: 2026-08-31

Scope: 未发布开发线的 doctor/next 行为、`task create` 多仓与门禁、`events.jsonl` 轮转、`--dry-run` / 复核退出码、`task close` 与 leftover receipt。不是 `task review` PASS，不是 Proof，不是发版。

SSOT: 当前工作区源码（`feat/dev_0814` 工作树，相对生产基线的未提交变更）。本记录不是交付门。

## Rules

1. 每位评审员只写自己的签字章节，不改写他人章节。
2. 源码和现场契约高于会话里的设计口头约定与同批席位意见。
3. 无法证明标 `须人工核`。
4. 仲裁只去重、裁定冲突、输出 P0/P1/P2 与 Go/No-Go。
5. 本会审不 merge、不 signoff、不发布。commit/push 是用户本轮另授的交付动作，不是会审授权。

## Frozen Baseline (2026-08-31)

| Object | Ref | SHA | Date |
| --- | --- | --- | --- |
| 生产基线 | `origin/main` | `f321e39df4b3994657e483d58e27d191a6765011` | 2026-08-23 |
| 当前开发线 | `feat/dev_0814`（与 `origin/main` 同 SHA，其上为本波未提交修复） | 同上 + working tree | 2026-08-31 |
| `origin/feat/dev_0814` | 落后本工作树所基于的 main | `6986f63de6bc65ee0a33cb6dc6d3fbe89368a22d` | — |
| 当前 `origin/release` | **不存在** | — | — |
| 历史 `origin/release/v0.6.1` | 历史线，不是当前生产 | `d90835e701658d31e78c196ab252825ead1c96f9` | — |

**不得默认 main 的说明：** 协议默认生产是 `origin/release`。本仓当前没有 `origin/release`，生产线按 `origin/main` @ `f321e39` 记录。`main` 相对该生产基线领先 0 commit。先前 delivery-physics 会审曾用当时的 `origin/main` 当已发布基线；本轮因缺少现行 `origin/release` 继续用 `origin/main`，并显式写出原因。

`origin/feat/dev_0814` 落后 `origin/main` 22 commit。本波是在已与 `origin/main` 对齐的 `feat/dev_0814` 上的行为修复，不是从过期远端功能分支分叉。

## 先前会审

已检索 `docs/reviews/` 与 `docs/superpowers/reviews/`。

- 同开发线、**同主题**（missing-origin / dry-run 诚实性 / `task close`）：**已检索·无先前会审**。
- 同仓不同主题：`docs/reviews/2026-08-19-slash-review-and-task-merge.md`（斜杠包装层）；`docs/superpowers/reviews/2026-08-15*` 与 `2026-08-16-delivery-physics-shipped-implementation-adversarial-review-board.md`（交付物理 / Proof）。那些 P0/P1 不在本波范围，**不记为本波已闭环**。

本目录沿用 `docs/reviews/`（与 2026-08-19 记录同一约定）。

## 席位

| Seat | Dispatch | Window | Status |
| --- | --- | --- | --- |
| security | 第一批派出 | ≥10 min，首派开始计时 | 交卷 ~15.7 min |
| silent-failure | 第一批派出 | 同上 | 交卷 ~11.3 min |
| python-cli | 第一批派出 | 同上 | 交卷 ~13.3 min |

无 `逾期未交`。主席在交卷后对席位主张做了源码复核，并修了交卷后仍成立的 P1（见仲裁「交卷后补丁」）。

---

# Security Review Section

Reviewer: security
Time: 2026-08-31
Verdict: Conditional Go

P0: none. P1: two (close confused-deputy; merge `--dry-run` mutates the line). Leftover `receipt.md` symlink delete-through and event-archive glob injection: 无 P0/P1 (disproved or fail-closed).

### SEC-P1-01 — `close_task` jail is check/use split; `--force` can retarget the line worktree

Severity: P1. Path: `src/dyro/tasks.py` remove loop vs jail only in validate loop.

Trigger: Task is `failed` (or `done`). Loop 1 sees a real task worktree. Before loop 2, `destination` is replaced with a symlink pointing at the line worktree. Then `task close --yes`.

Impact: Loop 2 did not re-run path jail. `git worktree remove --force` can realpath-match the line worktree.

Disprove attempt: Static symlink-at-mount and symlink-at-task-root are refused. Tests only cover that static case.

Must-fix: Re-apply path jail immediately before `worktree remove`.

### SEC-P1-02 — `task merge --dry-run` is a real merge on the line, not a no-op

Severity: P1. Path: `src/dyro/tasks.py` dry-run merge; `src/dyro/cli.py` `--yes` copy.

Trigger: `dyro task merge <id> --dry-run`.

Impact: Dry-run runs `git merge --no-ff --no-commit` on the line worktree, then abort. Overlay is not written; git is mutated until abort.

Disprove attempt: Product CHANGELOG discloses probe-and-abort. `--help` at seat time still said dry-run does not write Git.

### P2 (security)

- SEC-P2-01 local `run_gates(dry_run=True)` executes argv (documented).
- SEC-P2-02 leftover receipt unlink is POSIX-safe for symlink delete-through; leftover only deletes `task.directory/receipt.md`.
- SEC-P2-03 event archives name-scoped and fail-closed.
- SEC-P2-04 line-branch check does not walk parent symlinks.
- SEC-P2-05 review binding CLI `failed` ≠ task status `failed`.

Hunts 无 P0: leftover symlink delete-through disproved; event glob injection disproved as P0/P1; static close jail mitigated, TOCTOU was P1-01.

Independent check complete.

---

# Silent-Failure Review Section

Reviewer: silent-failure
Time: 2026-08-31
Verdict: Conditional Go

任务路径 9 项已闭环。席位交卷时：**开发线 merge/sync 仍把 `probed.append` 放在 `git()` 之后**。无 P0「CLI/overlay 把失败报成成功」。

### SF-1 — P1（交卷时开放）

位置：`src/dyro/workspace.py` `_merge_line_repositories_locked`。`line merge` / `line sync`（含 `--dry-run`）在 `git merge --no-ff --no-commit` 超时后，`probed.append` 在 `git()` 之后，当前仓可留下 `MERGE_HEAD`。CLI 不会假成功。任务 merge 路径当时已先 append 再 git。

### SF-2 — P2

`set_status` 写入 status 后 `append_event` 失败：状态文件已变；overlay 缺 `task_status` 行且在日志仍合法时 `complete=True`。CLI 不假成功。Ledger 有 `event_append_failed` + `error_code`。

### 已闭环（席位对照当时源码）

1. `set_status` 吞掉 `EventLogError` 无痕迹 — 已闭环（静默无痕迹）。Overlay 完整性见 SF-2。
2. 缺 current `events.jsonl` 却有 archive 当成空且 complete — 已闭环。
3. `_event_archive_files` 吃进 `.bak` — 已闭环。
4. 任务 merge dry-run 超时留下 `MERGE_HEAD` — 任务路径已闭环。开发线路径为 SF-1。
5. `close_task` `-d` 测 overlay HEAD — 已闭环（对 `line.branch` 做 ancestor，再 `-D`）。仍非多仓原子。
6. 绑定失败 raise 导致监督 UNCERTAIN — 已闭环（`return "failed"` → `ActionStatus.FAILED`）。
7. external dry-run 执行门禁 argv — 已闭环。
8. cwd-git 走到 overlay — 已闭环。
9. 重复 `diff-check` 名 — 已闭环。
10. leftover receipt symlink 跳过 — 已闭环（`unlink` 链接本身）。

Independent check complete.

---

# CLI Contract Review Section

Reviewer: python-cli
Time: 2026-08-31
Verdict: Conditional Go

五条指定契约在写路径上成立。交卷时剩下的是 CLI 诚实性缺口。

### 交卷时 P1

1. 全局 `--dry-run` 帮助仍声称不写 Git，但 `task merge --dry-run` 会真实 `git merge`。`src/dyro/cli.py` `_add_common`。
2. `task create --dry-run` 不执行空 adapters 拒绝。空 adapter 检查在 dry-run early-return 之后。

### 交卷时 P2

- `task merge --dry-run --push` 文案声称并推送，push 探测被 `git(..., dry_run=True)` 跳过。
- `task loop` / `task daemon` 对复核 `"failed"` 不非零退出。
- 事件归档 `str.isdigit()` 会把 Unicode 数字当归档名。
- `done` 关闭的 dirty / ancestor 门没有自动化测试。
- 轮转测试有一句恒真断言。

### 已闭环（席位）

1. `--dry-run` 不写 Dyro overlay。
2. 本地 gate dry-run 仍执行 argv；外部 dry-run 不执行 argv。
3. `task review` 拒绝（含 binding mismatch）非零退出；不 `set_status(failed)`。
4. 受监督 apply 把 `"failed"` 映射为 FAILED 而非 UNCERTAIN。
5. `task close`：done 要干净且已是 `line.branch` ancestor；failed 用 `--force`；拒绝 symlink mount。
6. 空 `verify` 每仓唯一门禁名；空 adapters 在写路径拒绝。
7. 事件轮转 `events.jsonl.<seq>`；忽略 `.bak`；缺 current 仍缝归档。

Independent check complete.

---

# Chair independent checks

主席在席位交卷后对照**当前**源码复核，不把席位票当事实。

1. 无现行 `origin/release`。生产基线 = `origin/main` `f321e39`。已确认。
2. `close_task` 静态 symlink / 任务根 symlink：源码 `_assert_task_worktree_path` 沿 leaf→root 拒绝 symlink，且 resolved 必须严格落在 worktree_root 下。测试 `test_task_close_refuses_symlink_mount_and_keeps_line_worktree`、`test_task_close_refuses_symlinked_task_root` 绿。
3. SEC-P1-01 双循环 TOCTOU：交卷时成立。交卷后 mutate 循环在 `worktree remove` 前再次 `_assert_task_worktree_path` + `_validate_task_worktree`。同源进程在两次循环之间替换路径会被第二次 jail 拦住。仍不是跨进程锁。
4. SEC-P1-02：本波产品契约就是 merge `--dry-run` 做真实 `--no-ff --no-commit` 探测再 abort（CHANGELOG / README 已写）。这不是「声称没跑 Git」。`--help` 交卷后已改成与 README 一致。不升为 P0，不阻断本波 commit。
5. SF-1 开发线 merge：交卷时成立。交卷后 `workspace.py` 改为先 `probed.append` 再 `git()`。测试 `test_line_merge_timeout_still_aborts_merge_head` 绿。
6. `task create --dry-run` 空 adapters：交卷后检查移到 early-return 之前。同一测试覆盖写路径与 dry-run，均 exit 2。
7. 事件归档 Unicode digit：交卷后 `suffix.isascii() and suffix.isdigit()`。
8. 恒真断言：交卷后改为 `records[0]["seq"] == 1`。

## 交卷后补丁（相对席位读到的树）

| 项 | 动作 |
| --- | --- |
| SF-1 开发线 merge 超时 | `workspace.py` 先登记 probed |
| python-cli P1 空 adapters dry-run | `cmd_task_create` 先拒绝再 dry-run return |
| python-cli P1 `--help` | `--dry-run` 帮助与 README 对齐 |
| python-cli P2 Unicode 归档名 | `isascii()` |
| python-cli P2 恒真断言 | 断言 seq 从 1 起 |
| SEC-P1-01 remove 前再 jail | mutate 循环再次校验路径 |

---

# Final Arbitration

Arbiter: 会审主席
Time: 2026-08-31

Final verdict: **Go for commit + push `feat/dev_0814`。No-Go for merge / tag / release / 生产。**

会审 Go 不构成 merge、signoff、发版。用户本轮另外授权了 commit 与 push。

## P0

无。没有已证实的「本 CLI 在操作员未确认时会自己 push / 发布 / 删掉开发线 worktree（静态路径）」路径。SEC-P1-01 的静态 symlink 已拒绝；交卷后 remove 前再 jail。同用户进程 TOCTOU 降为残余 P2：同一用户已能直接删 line worktree。

## P1（本波已闭环）

1. 未 push 的 `origin/<line.branch>` 不再把 next/start 卡成 needs_repair；doctor WARN。
2. `line spawn` 从父线本地 HEAD 起。
3. `task create --repository` 可重复；Profile `verify` 进门禁；空 verify 使用 `diff-check-<repo_id>`。
4. leftover `receipt.md` 在新一轮执行开始时删除（含 symlink）。
5. `events.jsonl` 按 `events.jsonl.<seq>` 轮转；读者忽略非 seq 后缀；缺 current 仍缝归档。
6. `set_status` 在事件追加失败时保留状态写入并 ledger `error_code`。
7. `task review` 绑定/哈希不匹配返回 `failed`，CLI 非零，监督层 FAILED 而非 UNCERTAIN；任务可留在 `review` 以便改 `review.md`。
8. 外部 Profile 的 gates `--dry-run` 不在本机执行 argv。
9. `task close` 拒绝 symlink mount / 任务根 symlink；done 要求干净且已合入 `line.branch`；failed 才 `--force`；remove 前再校验路径。
10. 任务 merge 与开发线 merge 的 dry-run 探测：先登记 probed，超时仍 abort `MERGE_HEAD`。
11. 空 adapters 在写路径与 `--dry-run` 均拒绝。

## P1（已接受残差，不阻断本波 push）

- **Merge `--dry-run` 会短暂改 line 的 index / `MERGE_HEAD` 再 abort。** 这是本波明确的冲突探测契约，不是静默成功。抛开探测改成 `merge-tree` / 一次性 clone 是后续产品选择，不是本波 P0。

## P2

- overlay 在 `event_append_failed` 后仍可对缺行日志报 complete（CLI 不假成功）。
- `task loop` / `task daemon` 对 review `"failed"` 仍退出 0。
- `task merge --dry-run --push` 不真正预检 push。
- done 关闭的 dirty / 未合入路径缺专项测试（实现已有门）。
- 本地 gates `--dry-run` 仍执行 argv（已文档化）。

## Go / No-Go

| 对象 | 结论 |
| --- | --- |
| 提交本波到 `feat/dev_0814` | **Go**（用户本轮授权 commit） |
| 推送到 `origin/feat/dev_0814` | **Go**（用户本轮授权 push；push ≠ 发布） |
| 合并进 `origin/main` / 打 tag / 发版 | **No-Go** |
| 用本会审代替 `task review` / Proof | **No-Go** |

## 测试证据

命令（无 pipeline 吞退出码）：

```text
.venv/bin/python -m unittest tests.test_review_remediation
```

摘要：`Ran 26 tests in 13.826s` / `OK`（交卷后、remove 再 jail 之前的全模块）。

```text
.venv/bin/python -m unittest tests.test_review_remediation tests.test_tasks tests.test_workspace tests.test_events tests.test_cli.StartTests tests.test_blueprint tests.test_continuation_supervision tests.test_hub
```

摘要：`Ran 215 tests in 109.964s` / `OK` / exit 0。含 remove 再 jail 之后的工作树。stdout 中有预期的 `错误：任务 T1 复核拒绝` 与 `错误：未配置任何 Agent adapter，无法创建任务`（断言非零退出）。

未执行：完整 `tests/` 全仓、CI、真实多仓工作区上的 `task close` 对抗替换。标 **未执行**。

`test_legacy_board_status_stays_on_protocol_id` 不在本命令内。历史会话曾在隔离运行时看到 JSON `UNSAFE_FILE`；本波未改 integrations，**须人工核** 是否为环境问题。

## 须人工核

1. Windows junction：`Path.is_symlink()` 对 junction 可能为假。本波在 macOS 上验证。
2. 真实 `subprocess.TimeoutExpired` 杀 git 子进程后，abort 是否总成功。测试用「merge 返回后再抛」模拟。
3. 把 merge dry-run 改成完全不碰 line worktree，是否值得下一刀产品变更。

## Next human Dyro command

无发明。本会审不查询未命名工作区的 `dyro next`。交付动作是用户已授权的 commit + push `feat/dev_0814`。
