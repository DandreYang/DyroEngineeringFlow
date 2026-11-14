# 开发线感知的 dyro next 与统一推送口径（设计草案 v3）

状态：草案 v3，按第二轮审查修订，未评审

证据来自当前 checkout。`pyproject.toml` 的版本是 `0.7.16`。现场反馈里的耗时、调用次数和提交数没有在本次复核里重测，下文标为现场口径。下文的行为分两类：标「当前」的是源码已有行为；其余是本草案的设计，尚未实现。

相对 v2，本版只闭合第二轮审查的四项：补上 `unconfigured` 并决定非 ready 的逐仓诊断；目录识别对全部 `ReadLimitCode` 用同一套信封；overlay 正文比较按 doctor 的重新抛出分路；显式工作区的 outside 理由排在线路径和任务路径之后。v1 已关闭的项不改写。

## 1. v1 失准处（已按源码更正）

1. **目录识别没有读预算。** 当前 `_line_from_directory`（`continuation/resolution.py:307-324`）调用无 `read_budget` 的 `list_lines`（`workspace.py:233-250` 只在传入预算时走 `_list_lines_bounded`），并调用 `list_tasks`（`tasks.py:416-422`），后者对每个 `task.toml` 做无界 `glob`。它不接收 `ReadBudget`，不能放进 JSON `next`。`resolve_line`（`resolution.py:327-353`）在非交互且候选不为 1 时直接失败，`next` 继续不用它。
2. **缺 `origin/<branch>` 的运行时产物只有 WARN。** `doctor` 在远端跟踪 ref 不存在时固定追加 `WARN <kind>:<id>/<repo>: missing <expected_remote>`（`workspace.py:1502-1508`），随后 `continue`，不会发 `FAIL`。`_MISSING_ORIGIN_FINDING`（`workspace.py:1532-1534`）把 `FAIL` 和 `WARN` 都写成可匹配。`cli.py:1769-1771`、`home.py:755-759`、`workspace.py:784-790` 在筛 `FAIL` 时排除这一形态，以当前 `doctor` 的产出来看匹配为空。函数说明（`workspace.py:1544-1551`）写明未发布是建议项，`next` 与 `start` 只因 `FAIL` 停止。`cmd_next` 的失败集是全部 `FAIL`（`cli.py:2844-2845`），没有这条排除。
3. **`_write_line` 会丢掉未进 `Line` 的表。** `Line` 是冻结数据类（`workspace.py:38-47`），字段没有远端锚点。`_write_line`（`workspace.py:73-99`）按字段重拼整份 TOML，`schema_version` 为「有 `parent` 则 3，否则 2」（`workspace.py:81`）。`_parse_line_content`（`workspace.py:102-141`）不保存未知表。只靠「解析器忽略未知表」不能让表活过下一次写回。可接受版本是 `{1, 2, 3}`（`workspace.py:25`）；不在集合内则 `ValidationError`（`workspace.py:109-110`）。
4. **doctor 的 ref 快照出函数即消失。** `observed`（`workspace.py:1451-1459`）只在 `doctor` 内使用。返回值是 `list[str]`（`workspace.py:1427-1528`）。已有的跨调用通道是挂在 `ReadBudget` 上的 `stash_observation_findings`（`workspace.py:1238-1243`）。`read_budget is None` 时该函数直接返回。JSON `next` 把同一预算对象传给 `doctor`（`cli.py:2841-2843`）；文本 `next` 传 `None`。
5. **远端 ref 已在、上游未配置、HEAD 与远端 SHA 不同：doctor 报 FAIL。** 对象表已有 `refs/remotes/origin/<branch>` 时不会走进 missing 的 WARN（`workspace.py:1502-1508`）。PASS 只在上游等于 `origin/<branch>`，或上游为空且 HEAD 非空且全文 SHA 等于该对象时成立（`workspace.py:1509-1515`）。上游为空且 HEAD 全文 SHA 与该对象不同时，落入 else：`FAIL <kind>:<id>/<repo>: expected upstream origin/<branch>, found -`（`workspace.py:1516-1519`；`expected_remote` 即 `origin/<branch>`，`workspace.py:332-333`）。领先、落后和分叉都是这条 FAIL，doctor 不区分。`cmd_next` 把任何 `FAIL` 放进 `needs_repair`（`cli.py:2844-2868`）。这条 FAIL 对不上 missing-origin 正则。

当前其余事实：`cmd_next` 不看工作目录落在哪条线，参数只有 `--format`。无 `FAIL`、已有开发线、且本机有可启动编码工具时，JSON 为 `state=ready`、`commands=[]`、`mutation_available=false`（`cli.py:2935-2949`）。文本在简报为空时打印「工作区已就绪。可用 dyro start 打开本机已安装的编码工具。」（`cli.py:2954-2955`）。JSON 另附 `family_unacked` 与 `push_policy_fields`。

`spawn` 不 fetch、不 push。`refs/remotes/origin/<branch>` 已存在时 `worktree add --track`（`workspace.py:391-400`）；不存在时 `--no-track`（`workspace.py:401-409`）。因此未发布的本地线会得到上面的 WARN。

`anchor-reference` 在核对上游之前就 `continue`（`workspace.py:1488-1495`），不产生 missing-origin WARN，其上游也不是本线的 `origin/<line.branch>`。

`status_rows`（`workspace.py:1295-1345`）对 anchor 和每条线的每个仓库做短状态。行里没有存储方式。没有上游时列是 `-`。命令仍成功。JSON doctor 在去掉路径时，把 `PASS repository …` 改写成 `repository …: ready`（`cli.py:588-589`），这个 ready 指 anchor 是 Git 仓库。Console 只投影 `FAIL`。现场说的「status 仍 ready」对应：`next` 的 `state=ready`、doctor 的 PASS 文案，以及 status 行里的 `upstream=-`。

`family_unacked` 是操作者跨全部家族的未读摘要，不阻止修复或 spawn。家族一层：父线、其直接子线、操作者；不含孙线。

推送句子今天有三套。工作区 overlay（`instructions.py:31`）禁止绕过门禁做 `git merge` / `git push`。线 overlay（`instructions.py:41-46`）要求必须跟踪 `origin/<branch>`。`PUSH_DISABLED_NOTE` 是「Dyro 不会 push；普通 `git push` 仍可用。」（`config.py:23`）。`allow_push` 为 true 时 `push_disclosure` 返回空串（`config.py:133-137`），`push_policy_fields` 不带 `push_note`（`config.py:140-143`）。

会执行 `git push origin <branch>` 的位置是线 merge/sync（`workspace.py`）和 `task merge`（`tasks.py`）。都要显式 `--push`，并且 `policy.allow_push`。本地合并成功后才逐仓 push。`--push` 前已有 `git push --dry-run`（`workspace.py:825-836`）。清单不写远端 SHA。

`seed_overlay_files` 在文件已存在且未 `--force` 时跳过（`instructions.py:94-96`）。`overlay_instruction_warning` 只在工作区文件缺失时发 WARN（`instructions.py:62-67`），`doctor` 调用它（`workspace.py:1439-1443`），不比较正文。

JSON doctor/status/next 从 45 秒天花板起算（`read_limits.py:66`，`cli.py:408-420`）。Bridge 默认 5 秒；每个额外 git scope 加 0.4 秒，封顶 45 秒（`read_limits.py:124-145`）。注释里的约 7 秒是约 58 个 worktree 的文本路径。`partial: true` 时退出码 2（`cli.py:2951-2952`）。控制面技能把非零退出和 partial 观察视为受阻。

## 2. 开发线感知的 next

### 目标

- 判断工作目录落在哪条线，判断过程接受 `ReadBudget`。
- 报告这条线的 id、父线、直接子线 id、逐仓上游、ahead/behind、未推送数。
- `mutation_available` 为 false 时给出稳定原因码。
- 非交互运行的 ready 文本、`needs_agent` 文本和 JSON `summary` 不出现 `dyro start`。
- `--line` 与工作目录不一致时警告，观察对象仍是 `--line`。

### 不做

- 不把 `ready` 改成会突变。`next_commands()` 在已有开发线时仍返回 `[]`。`commands` 不含 `line sync`、`line merge`、`push`。
- 不 fetch，不把 `status_rows` 并进 `next`，不做网络检查。
- 不做兄弟分叉，不做父线提交差，不在 `line_context` 里放频道预览。顶层 `family_unacked` 保留。有未读时，`diagnostic_commands` 可附加 `line inbox --family <家族> --unacked`。该字符串不是 `commands`。
- 不新增 `line spawn --track`。远端 ref 已在时的 `--track`、不在时的 `--no-track` 保持 `workspace.py:391-409`。
- 不改 Console 的 FAIL-only 投影。孙线不进入本对象。`anchor-reference` 不参与 ahead/behind，也不参与 `push_ready` 的「是否已跟踪」判断。

### 目录识别

新增带预算的目录识别，只给 `next` 用。现有 `_line_from_directory` 留给交互命令。

- 开发线来自 `list_lines(config, read_budget=budget)`。`doctor` 已经读过这份清单（`workspace.py:1450`）。实现时把这份 `list[Line]` 用与 `stash_observation_findings` 相同的方式挂到同一 `ReadBudget` 上，`next` 优先读这份快照，避免再扫一遍清单目录。
- 任务工作树使用 `list_task_ids_bounded`（`tasks.py:425`）和 `load_task_bounded`（`tasks.py:283-309`），`known_line_ids` 来自上面的清单。用 `worktree_root`（`tasks.py:1272-1273`）做路径包含判断。禁止调用 `list_tasks`。
- 路径判断沿用 `relative_to`。父线目录只命中父线。任务工作树的命中不要求 cwd 落在某个 `line_root` 里。
- 有 `--line`：观察对象是该 id。工作目录命中的线与它不同时 `location.disagree=true`。这一判断使用下面已经完成的命中集合。

`needs_repair` 今天在目录识别之前就返回（`cli.py:2846-2868`）。实现把带预算的目录识别挪到这次返回之前，仍不调用 `for-each-ref` 或 `worktree list`。doctor 已因非截止 `ReadLimitError` 抛出时，走 doctor 自己的路径，到不了目录识别。

### 原因码的顺序

先完成两类路径，再写 `location.reason`。顺序固定：

1. 对已在手上的每条线做 `line_root` 的 `relative_to`。
2. 再对任务做 `worktree_root` 的 `relative_to`，命中则并入该任务的线。
3. 两步都有了结果之后才选定原因：
   - 线清单这一步被任一 `ReadLimitCode` 打断：`line_resolution_limited`。
   - 线清单这一步完成，任务这一步被任一 `ReadLimitCode` 打断：`task_scan_incomplete`，并设 `location.task_scan=partial`。
   - 两步都完成且一条命中：它就是目标线。显式 `--workspace` 下的任务工作树走这条，原因码停留在这条线上。
   - 两步都完成且多条命中：`ambiguous`，只列候选 id。
   - 两步都完成、零条命中、并且这次命令行带有 `--workspace`、且 cwd 用 `relative_to` 对不上该工作区根：`cwd_outside_selected_workspace`。
   - 两步都完成且零条命中的其余情况：`cwd_not_in_line`。工作区级状态仍是今天的 `needs_repair` / `needs_line` / `needs_agent` / `ready`。

从 cwd 推断出来的工作区不算显式 `--workspace`。那种零命中用 `cwd_not_in_line`。

`cwd_not_in_line` 与 `cwd_outside_selected_workspace` 只出现在第 3 步，且两步都完整结束。任务路径上的命中先成为「一条命中」，outside 不参与。

打断前已经 `relative_to` 成功的 id 留在 `location.cwd_lines`。引发错误的那一条不计入。这些 id 是已知命中，不升格为唯一目标线，`line_context` 为 `null`。`bounded_directory_names` 在记录数达到上限时直接抛出，不把已扫到的名字返回给调用方（`read_limits.py:686-689`），所以这一次枚举本身不新增命中；此前另一步已经完成的命中仍然保留。

### 目录识别的读限制

`ReadLimitCode` 共五个（`read_limits.py:18-23`）：`FILE_TOO_LARGE`、`AGGREGATE_BYTES_EXCEEDED`、`DEADLINE_EXCEEDED`、`RECORD_LIMIT_EXCEEDED`、`UNSAFE_FILE`。目录识别对五个 code 用同一套规则，在 `cmd_next` 内接住并先打印 JSON，再 `SystemExit(2)`（与简报截止的写法相同，`cli.py:2951-2952`）。不回退到无界 `list_tasks` 或无预算的 `list_lines`。

这张表只描述 doctor 已经返回之后、目录识别自己抛出的错误。doctor 内部的错误仍走下一节的 doctor 路径，不进这张表。

| Code | 信封 | `partial` | `code` | 退出码 | 已知命中 |
| --- | --- | --- | --- | --- | --- |
| `FILE_TOO_LARGE` | `next_step` | `true` | `FILE_TOO_LARGE` | 2 | 留在 `cwd_lines`；`line_context` 为 `null` |
| `AGGREGATE_BYTES_EXCEEDED` | `next_step` | `true` | `AGGREGATE_BYTES_EXCEEDED` | 2 | 同上 |
| `DEADLINE_EXCEEDED` | `next_step` | `true` | `DEADLINE_EXCEEDED` | 2 | 同上 |
| `RECORD_LIMIT_EXCEEDED` | `next_step` | `true` | `RECORD_LIMIT_EXCEEDED` | 2 | 同上；本次枚举不新增命中 |
| `UNSAFE_FILE` | `next_step` | `true` | `UNSAFE_FILE` | 2 | 同上；不安全的那一条不进入 `cwd_lines` |

`code` 写成实际抛出的那个枚举值。目录识别不要调用 `_observation_timeout_fields`（`cli.py:597-600`）：那个 helper 在 `partial` 时固定写成 `DEADLINE_EXCEEDED`。

`mutation_unavailable_reason` 不因这张表改写 `needs_repair`：bootstrap 不适用时仍是 `repair_not_applicable`，适用时 `mutation_available` 仍为 true 并省略原因字段。目录识别把一个本来可以 `ready` 的结果变成受限时，原因码用 `partial_observation`。`partial` 与 `code` 与原因码并行出现。`unconfigured` 本身不把退出码改成 2。

文本 `next` 今天没有预算。文本识别单独建一个只读清单和任务清单的 `ReadBudget`，不跑 Git。五个 code 同样处理：不把已知命中说成「当前线」，目录线视为未识别，文本退出码保持今天的成功路径。文本没有 JSON 信封，也不设 `partial`。

doctor 尚未返回就抛出的 `ReadLimitError` 维持当前行为。`DEADLINE_EXCEEDED` 由 doctor 收成 deadline finding 并返回（`workspace.py:1525-1526`），JSON 的 `code` 仍是 `DEADLINE_EXCEEDED`。其余 code 在保存 findings 后重新抛出（`workspace.py:1521-1524`），JSON 为 `kind=error`、退出码 2（`cli.py:820-835`、`cli.py:6093-6096`）。

### 把 doctor 的观察交出来

`doctor` 在离开 `observed` 之前，把每个 `repo_id` 的三样东西挂到传入的 `ReadBudget`：`_worktree_inventory` 的 path → `(head, branch)`、`_ref_inventory` 的上游表、引用全名 → 对象 ID。截止或重新抛出 `ReadLimitError` 之前，把已经填好的部分一并挂上，与 findings 的 stash 同一时机（`workspace.py:1521-1528`）。

`cmd_next` 只读这个挂载。不再调用 `for-each-ref` 或 `worktree list`。文本路径的 `read_budget` 是 `None`，挂载为空，ahead/behind 全部为 `null`。文本仍可根据返回的 findings 里、目标线上的 `is_missing_origin_finding` 说出「尚未发布」。

### JSON

控制面信封的 `schema_version` 仍为 1（`cli.py:559`）。这与开发线清单版本不是同一个数。路径不出现。`mutation_available` 为 false 时增加 `mutation_unavailable_reason`：

| 场景 | 原因码 |
| --- | --- |
| `workspace_missing` | `workspace_missing` |
| `needs_repair` 且 bootstrap 不适用 | `repair_not_applicable` |
| `needs_agent` | `no_launchable_agent` |
| `ready` | `observation_only` |
| 目录识别或计数把观察变成受限，且状态不是 `needs_repair` | `partial_observation` |

`needs_line` 和可 bootstrap 的 `needs_repair` 仍为 `mutation_available=true`，省略原因字段。

`ready` 或 `needs_repair`，且上面两步都完成、目标线唯一时，增加 `line_context`。计数未知时为 `null`。`unconfigured` 在当前 doctor 里是 FAIL，工作区状态是 `needs_repair`；逐仓诊断因此要能出现在 `needs_repair` 上。`needs_line`、`needs_agent`、`workspace_missing` 不加 `line_context`。`workspace_missing` 没有 Profile。`needs_agent` 表示没有 doctor FAIL，到不了 `unconfigured`。

`line_context` 不改变 `state`、`commands`、`mutation_available` 或 findings。它不把 FAIL 收成 `ready`。识别被打断时目标线不算唯一，`line_context` 为 `null`，`cwd_lines` 仍保留已知命中。

```json
{
  "location": {
    "cwd_lines": ["core"],
    "targeted_line": "core_pay",
    "disagree": true,
    "reason": "line_flag"
  },
  "line_context": {
    "id": "core_pay",
    "kind": "line",
    "branch": "feat/core_pay",
    "parent": "core",
    "children": [],
    "push_ready": false,
    "repositories": [{
      "id": "api",
      "storage": "linked-worktree",
      "expected_upstream": "origin/feat/core_pay",
      "upstream": "",
      "upstream_state": "missing",
      "ahead": null,
      "behind": null,
      "unpushed_commits": null
    }]
  }
}
```

计数只使用挂载里已有的对象，外加下面的有限次本地 `rev-list`：

- `upstream_state`：
  - `missing`：`refs/remotes/origin/<line.branch>` 不在对象表。doctor 报 WARN 并 `continue`（`workspace.py:1502-1508`）。
  - `ok`：上游正是该 ref。
  - `same_sha_unconfigured`：没有上游配置，且 worktree HEAD 的全文 SHA 等于该 ref。这与 doctor 的 PASS 条件一致（`workspace.py:1509-1515`）。
  - `unconfigured`：该 ref 在对象表中，上游配置为空串，HEAD 全文 SHA 非空且不等于该 ref。已领先、落后和分叉都是这一态。今天 doctor 报 `FAIL <kind>:<id>/<repo>: expected upstream origin/<branch>, found -`（`workspace.py:1516-1519`）。HEAD 与远端对象都在，所以不记 `unknown`，也不并进 `mismatch`。
  - `mismatch`：上游配置非空，且不是该 ref。同一条 FAIL 的 `found` 后面是这个别的 ref。
  - `not_applicable`：`anchor-reference`。
  - `unknown`：HEAD 为空，或对象表缺少比较所需的 SHA。上游为空且 HEAD 为空时，doctor 同样落入 else 并报 FAIL `found -`（`workspace.py:1510-1519`）；状态仍记 `unknown`，计数为 `null`。
- HEAD 全文 SHA 与远端跟踪对象全文 SHA 相同：`ahead=0`、`behind=0`、`unpushed_commits=0`，不起进程。`unconfigured` 在两个 SHA 都在且不同时，仍走下一条，不把计数强制写成 `null`。
- 两者都在、且不相同：至多一次 `git_read`（`process.py:150-170`）执行 `rev-list --left-right --count <remote_sha>...<head_sha>`，参数是已观察到的全文 SHA。禁止 `fetch`、`ls-remote`、`push`，以及会访问远端的 refspec。`unpushed_commits` 等于这次得到的 ahead。进程非零则三者为 `null`。
- 整次 `next` 这种 `rev-list` 硬顶 **4** 次，且只针对目标线的 `linked-worktree`。超出的仓库保持 `null`，并标 `counts_omitted=true`。调用前先看 `remaining_seconds()`（`read_limits.py:354-368`）；剩余不足 1 秒或将要超过截止，则不起进程，记 `null`。禁止把没算完写成 0。
- `push_ready`：只看 `linked-worktree`。每一仓都是 `ok` 或 `same_sha_unconfigured` 时为 true；任一仓 `missing`、`mismatch` 或 `unconfigured` 时为 false；任一仓 `unknown`，或 doctor 快照不完整时为 `null`。全部仓库都是 `anchor-reference` 时为 `null`。`anchor-reference` 不因「上游不是本线分支」把整条线打成 false。
- `children` 是清单里 `parent` 等于目标线的 id，来自已挂载的 `list[Line]`，不再读盘、不跑 Git。

文本在目标线唯一时以「当前线 `<id>`」起句，`ready` 与 `needs_repair` 相同。`needs_repair` 随后仍逐条打印今天的 FAIL。未发布、未读条数非空才附上。`disagree=true` 时先打印一行：工作目录落在甲线，观察目标是乙线。非交互判定仍是 stdin 与 stdout 都是 tty。交互、简报为空、且两步都完成并得到 `cwd_not_in_line` 时，保留今天的 start 句子。

### 预算与 status 上的 push_ready

JSON `next` 继续使用已经跑完 `doctor` 的那只 45 秒预算。增量 Git 只有上面最多 4 次本地 `rev-list`。预算耗尽时已完成的仓库保留数字，其余为 `null`，`partial=true`，退出码 2。这里的 `code` 同样写成实际的 `ReadLimitCode`，与目录识别那张表一致。原因码在状态不是 `needs_repair` 时为 `partial_observation`。

文本不做这 4 次 `rev-list`，计数为 `null`，退出码不改成 2。

`status` 不新增 Git。JSON 增加 `line_push_ready`：`[{id, kind, push_ready}]`。某一行是否计入，用同一预算读到的 `Line.storage_for` 决定，忽略 `anchor-reference`。`linked-worktree` 的 `upstream` 为 `-`，或不是该线的 `origin/<branch>`，则该线为 false。一条线全是 `anchor-reference` 时为 `null`。status 行没有远端对象 SHA，所以 doctor 视为 PASS 的 `same_sha_unconfigured` 在 status 里仍是 false。`unconfigured` 在 status 里也是 false：它的上游列同样是 `-`。更细的判断只放在 `next` 的 doctor 快照上。

`state=ready` 保持「没有 doctor FAIL」。未发布用 `push_ready=false` 表达。`unconfigured` 因为是 FAIL，工作区状态保持 `needs_repair`，同时 `push_ready=false`。

## 3. 统一推送口径

### 三处同一段话

`next` 的 `push_note`、工作区 overlay、开发线 overlay、控制面技能资产使用下面三段，字节级相同。`allow_push` 为 true 时也返回这段，不再返回空串。

```text
Dyro 默认不执行 push。只有 `policy.allow_push = true` 时才推送：`dyro line push` 推开发线分支；显式带 `--push` 的命令在本地合并成功后逐仓执行 `git push origin <分支>`。多仓 push 不是原子操作。
开发线的期望上游是 `origin/<line.branch>`。本地还没有这条远端分支时，doctor 给出 WARN，工作区仍可打开；这不表示可以推送。
普通 `git push` 仍然能改远端，但不会写入 Dyro 的远端锚点，也不算通过门禁。
```

技能在三段之后保留座位规则：控制面座位不执行 merge 或 push。这段只放在技能里。线模板仍写 id 和分支名，删掉「必须跟踪」。JSON 增加 `push_contract_version: 1`。`allow_push` 布尔值保留。

doctor 的 WARN 原文保持 `WARN <kind>:<id>/<repo>: missing origin/<branch>`，以便现有正则继续命中。不把这一形态改成 `FAIL`。

### 已铺到磁盘的 overlay

`doctor` 在现有「文件缺失」WARN 之外，对**已经存在**的工作区 `AGENTS.md` / `CLAUDE.md` 做正文比较：用当前 `render_workspace_overlay()` 的字节（与 `seed_overlay_files` 一样补上末尾换行）比对预算内读到的文件。不一致则发一条 WARN，形如 `WARN overlay workspace AGENTS.md 与当前模板不一致；运行 dyro host seed --force`，`CLAUDE.md` 各一条。开发线目录上已存在的两个文件，与当前 `render_line_persona(line_id, branch)` 比较，WARN 带线 id，不带绝对路径。文件仍缺失时保持今天的缺失 WARN。线目录缺少这两个文件时不新增 WARN。

比较紧接在现有 `overlay_instruction_warning`（`workspace.py:1439-1443`）之后，仍在 `list_lines`（`workspace.py:1450`）之前。每次只包住这一份文件的读取。局部降级不改变这个位置；需要继续抛出的错误因此不会在预算已经耗尽之后再去读清单和 Git。

单文件上限在计入聚合字节之前判断（`read_limits.py:541-544`，以及 `read_limits.py:482-491`）。文件超过自身上限时得到 `FILE_TOO_LARGE`，不会先变成 `AGGREGATE_BYTES_EXCEEDED`。

| 这一次读取的结果 | 正文比较 | 随后的 doctor |
| --- | --- | --- |
| `FILE_TOO_LARGE` | 该文件一条 WARN「无法核对」。不当成一致，也不当成缺失 | 不向外抛，doctor 继续 |
| `UNSAFE_FILE`，且就是这一份 overlay 文件（非常规文件，或读取过程中变了） | 同上 | 不向外抛，doctor 继续 |
| 普通读失败（`OSError`，或不是 UTF-8） | 同上 | 不向外抛，doctor 继续 |
| `AGGREGATE_BYTES_EXCEEDED` | 不捕获，不发「无法核对」 | 落到现有 `except`：保存 findings 后重新抛出（`workspace.py:1521-1524`）。JSON 为 `kind=error`，`code=AGGREGATE_BYTES_EXCEEDED`，退出码 2 |
| `DEADLINE_EXCEEDED` | 不捕获，不发「无法核对」 | doctor 收成 deadline finding 并返回（`workspace.py:1521-1526`）。JSON `partial=true`，`code=DEADLINE_EXCEEDED`，退出码 2 |
| `RECORD_LIMIT_EXCEEDED` | 单文件读取不产生这个 code。比较函数不捕获它 | 与 aggregate 相同：保存 findings 后重新抛出，JSON `kind=error`，退出码 2 |

`doctor` 不写文件，不自动刷新。本地改过的 overlay 只有维护者执行 `dyro host seed --force` 才被模板覆盖（`instructions.py:94-101`，工作区与全部线见 `seed_configured_overlays`，`instructions.py:139-161`）。

口径到达现有工作区的路径：

- 新二进制的 `push_note` 在下一次 `next` / `status` 就生效，不依赖磁盘上的旧 markdown。
- 磁盘上的 `AGENTS.md` / `CLAUDE.md` 维持旧句，直到有人执行 `dyro host seed --force`。doctor 的新 WARN 让这个差异可见。
- 仓库里的控制面 `SKILL.md` 要靠既有的 `dyro host compile` 进工作区副本。本草案不让 doctor 比较或重写已编译技能。

### push 成功后的远端 SHA

在 `workspace.py` 与 `tasks.py` 两处，每一仓 `git push` 成功后，把该仓 HEAD 的全文 SHA 写入该线的 `[remote_anchors]`。只记录 `linked-worktree`。`anchor-reference` 不写入。后一仓失败时，已成功仓的锚点保留。清单写入失败要报出来，因为 push 已经发生。

持久化规则：

- `Line` 增加 `remote_anchors: Mapping[str, str]`，默认空。
- 表非空时 `schema_version = 4`，并且 `[remote_anchors]` 一定写出。表为空时仍是「有 parent 则 3，否则 2」，不写这张表。有 parent 且有锚点时版本是 4，parent 字段照常写出。
- `LINE_MANIFEST_SCHEMAS` 加入 4。`_parse_line_content` 仅在版本 4 读取该表。键必须是该线 `repositories` 的子集，值必须是全文 SHA。版本低于 4 却出现该表：拒绝加载。
- 旧 Dyro 遇到版本 4 会在 `workspace.py:109-110` 拒绝解析，因此旧的 `_write_line` 不会拿到 `Line`，也不会把表抹掉。代价是：第一次写下锚点之后，旧二进制不能再加载这条线。没有锚点的线保持版本 2 或 3，旧二进制仍可读写。
- 新的 `_write_line` 从字段写出该表。任何会持久化的 `Line(` 构造都必须带上读到的 `remote_anchors`。默认空映射只用于新建的线。漏带字段再写回，新二进制自己也会抹掉该表。

不新增名为 `line publish` 的命令。顶层线和子线用 `dyro line push` 发布自己的分支：先确认 `policy.allow_push`，再取与 `line merge --push`、`retire` 相同的 L1 合并锁；再按仓库 ID 升序取 L2 anchor 锁；锁内重读清单，并拒绝该线任何未完成的操作（merge、merge-into-base、retire）。只推 `linked-worktree`。全部这类仓库预检通过后（工作树存在且是 Git、当前分支就是线分支、没有进行中的 Git 操作、同一 refspec 的 `git push --dry-run` 成功），才按清单顺序执行 `git push -u origin refs/heads/<branch>:refs/heads/<branch>`（显式同名 refspec，`push.default=upstream` 也不能把目标改到别的远端分支；`-u` 同时把上游纠正为 `origin/<branch>`）。不使用 `--force`、`--force-with-lease` 或 `+refspec`，也不要求工作树干净，只推已提交内容。成功一仓就为该仓写 `[remote_anchors]`，取值是推送前后各读一次、两次一致的 `refs/heads/<branch>` tip；不用事后的 HEAD（工作树可能已切走），也不用 `refs/remotes/origin/<branch>`（窄 fetch refspec 下不会更新）；推送期间分支移动则不写该仓锚点并报错；`anchor-reference` 跳过、不写锚点。跨仓不原子：后一仓失败时，已成功仓的锚点保留。锚点写入失败要报出来，因为 push 已经发生。`spawn` 不发布。终端里的普通 `git push` 不写 `[remote_anchors]`。`policy.allow_push = true` 时，`line merge` / `line sync` / `task merge` 仍可显式加 `--push`。

## 4. 延期

- `dyro line push-ready`：`ls-remote` 要走网络，只能证明读得到 ref，不能证明写权限；`--push` 前已有本地 `git push --dry-run`（`workspace.py:825-836`）。
- `dyro line reanchor`：改写历史后 Change Set 与 `task-heads.json`（常量 `tasks.py:86`）仍指向旧 SHA，本草案不迁移这些记录，因此不提供改清单锚点的命令。

## 5. 审查开放问题的答复

1. **旧 overlay 用 WARN，不自动刷新。** doctor 只报告与当前模板的字节差异，并指向 `dyro host seed --force`。自动覆盖会改掉操作者改过的文件，也会把写操作放进 doctor。
2. **`next` 不做跨分支的提交图遍历。** ahead/behind 只用 doctor 已经挂到 `ReadBudget` 上的 ref。SHA 相同则写 0。不相同则最多 4 次本地 `rev-list`，否则 `null`。兄弟比较和父线提交差不进入 `next`。
3. **发布走 `dyro line push`，不新增 `line publish`。** 见第 3 节末。`spawn` 不代跑首次发布。
4. **有锚点表的清单升到 schema_version 4；空表不升。** 防脱落靠两件事：字段进入 `Line` 并由 `_write_line` 写出；旧客户端因版本 4 无法解析而无法走旧的整文件重写。控制面 JSON 的 `schema_version` 保持 1。

第二轮的四项由第 2 节和第 3 节回答。这里不重开上面四条。

## 6. 测试

- 带预算的目录识别：父线目录、子线目录、任务 worktree 各命中预期的线；多条命中为 `ambiguous` 且进程成功。
- JSON `next` 的识别路径不调用 `list_tasks`，也不调用无预算的 `list_lines`。五个 `ReadLimitCode` 都不回退到无界扫描。JSON 信封是 `next_step`，`partial=true`，`code` 等于该 `ReadLimitCode`，退出码 2，`line_context` 为 `null`。打断前已完成 `relative_to` 的 id 留在 `cwd_lines`。线清单未完成时 reason 为 `line_resolution_limited`。只有任务扫描未完成时 reason 为 `task_scan_incomplete`，`task_scan=partial`。这两种 reason 都不是 `cwd_not_in_line`，也不是 `cwd_outside_selected_workspace`。`RECORD_LIMIT_EXCEEDED` 这次枚举不新增命中。
- 文本 `next` 对这五个 code 同样不宣布当前线，退出码保持今天的成功路径。
- 显式 `--workspace`，cwd 在该工作区的任务 worktree 上、不在任何 `line_root`：命中任务所属线。cwd 对不上该工作区根，并且线路径与任务路径都未命中：reason 为 `cwd_outside_selected_workspace`。从 cwd 推断出的工作区在零命中时仍是 `cwd_not_in_line`。
- `--line` 与工作目录不同：`disagree=true`，计数属于 `--line`。
- 非交互文本与 JSON summary 不含 `dyro start`；交互、两步都完成且得到 `cwd_not_in_line`、简报为空时仍含今天那句。
- `ready` 的原因码为 `observation_only`，`next_commands()` 仍为 `[]`。
- 无 `origin/<branch>`：ahead/behind/unpushed 为 `null`，`push_ready=false`，finding 仍是 WARN 且能被 `is_missing_origin_finding` 命中，`next` 不因此进入 `needs_repair`。
- 远端 ref 已在、上游为空、HEAD 全文 SHA 与远端对象不同：`upstream_state=unconfigured`，`push_ready=false`，ahead/behind 仍按 SHA 规则计算。doctor finding 仍是 `FAIL … expected upstream origin/<branch>, found -`，`state=needs_repair`，目标线唯一时 JSON 仍带 `line_context`。退出码不因此改成 2。SHA 相同仍是 `same_sha_unconfigured`。上游指向别的 ref 仍是 `mismatch`。HEAD 为空为 `unknown`，计数为 `null`。
- SHA 相同不调用 `rev-list`。第 5 个需要计数的仓库为 `null` 且 `counts_omitted=true`。进程失败或剩余时间不足 1 秒时不为 0。计数阶段的 `ReadLimitError` 把 `code` 写成实际枚举值。
- `anchor-reference` 的 `upstream_state=not_applicable`。一条混合线的 `push_ready` 只由 `linked-worktree` 决定。全是 `anchor-reference` 时 `push_ready` 为 `null`。status 的 `line_push_ready` 不因 `anchor-reference` 的 `origin/main` 变成 false，且不新增 Git 调用。
- `line_context` 不含兄弟列表和频道 preview。顶层 `family_unacked` 仍在。`needs_line`、`needs_agent`、`workspace_missing` 不带 `line_context`。
- doctor 对与模板不同的已有 `AGENTS.md` / `CLAUDE.md` 发 WARN；缺失文件仍是原来的缺失 WARN；doctor 不改文件。不带 `--force` 的 seed 保持原字节。
- 已有 overlay 超过单文件上限（`FILE_TOO_LARGE`），或这一份文件为 `UNSAFE_FILE`：一条「无法核对」WARN，doctor 继续并返回其余 findings。`AGGREGATE_BYTES_EXCEEDED` 与 `DEADLINE_EXCEEDED` 不降级成这条 WARN。截止仍由 doctor 收成 partial 返回；aggregate 在保存 findings 后重新抛出，且不会继续读清单和 Git。
- 三段口径在 `push_note` 与两套模板中逐字相同。`allow_push=true` 时 `push_note` 仍非空。
- 线 merge `--push` 成功后，`remote_anchors` 等于该仓 HEAD，清单为 schema_version 4。第二仓失败时第一仓锚点仍在。`anchor-reference` 不出现在表里。无锚点的旧清单仍是版本 2 或 3 且能加载。版本 4 能往返 `_parse_line_content` 与 `_write_line` 而不丢表。构造 `Line` 时漏掉该字段再写回，测试要失败。

## 7. 实现边界

复用 `Line`、doctor 的 WARN 原文、`_ref_inventory`、`_worktree_inventory`、现有 `ReadBudget` 挂载、`list_lines` / `list_task_ids_bounded` / `load_task_bounded`，以及 45 秒 JSON 预算。不新增子系统。实现不在本草案范围内。
