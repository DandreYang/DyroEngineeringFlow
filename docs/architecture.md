# 架构与 Profile 契约

## 分层

```text
DyroEngineeringFlow Core（`dyro` CLI）
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

[repositories.api]
path = "repositories/services/api"
mount = "services/api"
remote = "git@example.com:group/api.git" # 可选；仅供 bootstrap clone 缺失 anchor
verify = [["python3", "-m", "pytest", "-q"]]

[adapters.codex]
launch = ["codex", "-C", "{workspace}"]
read = ["codex", "exec", "--sandbox", "workspace-write", "{prompt}"]
write = ["codex", "exec", "--sandbox", "workspace-write", "{prompt}"]
```

可用占位符仅为 `{workspace}`、`{root}`、`{task}`、`{line}`、`{prompt}`。命令必须写为 argv 数组；Profile 若通过 `sh -c` 等方式绕过这一约束，安全责任由 Profile 维护者承担。

`read` adapter 需要写出 `review.md`，因此部分 Agent 不能使用完全只读的进程沙箱。Core 会在复核前后重新核对每个任务仓库的 clean 状态与固定 HEAD；任何源码变动都会使复核失败，不能进入 `done`。

## 运行态

不把每次运行产生的状态塞回主配置。DyroEngineeringFlow 在工作区中使用：

```text
.dyro/
  lines/<id>.toml       功能开发线登记（逐仓 base 与 storage mode）
  hotfixes/<id>.toml    Hotfix 登记
  tasks/<id>/task.toml  任务机读契约
  tasks/<id>/handoff.md 人类规格
  tasks/<id>/receipt.md 执行回执
  tasks/<id>/task-heads.json 执行完成时的逐仓 Git HEAD
  tasks/<id>/review.md  独立复核裁决（绑定 receipt 与 task-heads）
  tasks/<id>/signoff.json 外部签收记录（可选策略）
  changes/<id>.toml     跨仓交付 Change Set（逐仓不可变 HEAD）
  decisions.toml        决策点（blocked_on）
  ledger.jsonl          追加式审计台账
```

状态机为：

```text
backlog → assigned → in_progress → review ───────────────→ done
                         ├→ waiting_answer → in_progress       ▲
                         ├→ failed → assigned                   │
                         └→ review_pending_signoff ─────────────┘
```

非法状态跳转会被拒绝；人工恢复必须显式使用 `--force`。

## 外部隔离执行契约

当 Profile 使用 `execution_mode = "external"` 时，Dyro 不会在控制机上启动 Agent、执行 gates、复核或合并。受信任的 runner 通过显式证据接口与控制面交接：

```bash
dyro task claim TASK-42 --by runner-2026-10-01
# runner 在它自己的隔离工作区执行任务与 gates
dyro task evidence execution TASK-42 \
  --receipt /runner/out/receipt.md \
  --gates /runner/out/gates.json \
  --heads /runner/out/task-heads.json
# 独立复核也在隔离环境完成
dyro task evidence review TASK-42 --file /review/out/review.md
dyro task signoff TASK-42 --by release-manager  # 仅 require_external_signoff = true 时
```

领取记录采用文件的原子创建，避免两个 runner 同时获得同一任务。执行证据中的 `gates.json` 使用以下通用格式；每个声明的任务 gate 都必须存在、退出码为 0、日志位于 JSON 同目录内，并且日志哈希必须匹配：

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

导入后，Dyro 会保存回执、门禁 JSON、日志和 HEAD 证据副本。复核文件首行仍为 `verdict: PASS` 或 `verdict: FAIL`，并且必须同时包含 `receipt_sha256: <hash>` 与 `task_heads_sha256: <hash>`；任一不匹配时任务继续保持在 `review`。外部 runner 的认证、容器、云平台或审批系统由 Profile 扩展实现，核心只定义不可绕过的交接证据。

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

## 扩展路线

未来的 adapter、通知、签名规则、发布平台与审批系统应使用 Python entry point 或独立 Profile 扩展包接入；不要把某个组织的策略加入 core 默认行为。
