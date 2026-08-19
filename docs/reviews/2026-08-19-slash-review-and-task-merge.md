# Dyro 斜杠命令 会审 / 对抗审查

Date: 2026-08-19

Scope: 本会话新增的用户斜杠 `/dyro-task-merge` 与 `/dyro-review-board`（skills-library），不是 Huiyichu 产品线，也不是 `dyro` 包装源码。

SSOT: 当前 skill 文件 + 已安装 `dyro-board` + `dyroengineeringflow` 中 `merge_task` / `explain_task` / `cmd_next`。本记录不是 Proof，不是 `task review` PASS。

## Rules

1. 每位评审员只写自己的签字章节，不改写他人章节。
2. 源码和现场契约高于会话里的设计口头约定。
3. 无法证明标 `须人工核`。
4. 仲裁只去重、裁定冲突、输出 P0/P1/P2 与 Go/No-Go。
5. 本会审不 merge、不 push、不 signoff、不发布。

## Frozen Baseline (2026-08-19)

| Object | Note |
| --- | --- |
| `dyro-task-merge` | 用户斜杠；`disable-model-invocation: true` |
| `dyro-review-board` | 用户斜杠；指向 first-party `dyro-board` |
| first-party `board` | `dyro integration status board` → `current` |
| `dyro-engineering` next | `needs_repair`；`commands=[]`；`mutation_available=false`；线 `dev_0814` expected `feat/dev_0814` found `chore/release-0.7.5` |

## Reviewer Sections

- A 安全与契约：[sections/01-task-merge.md](2026-08-19-slash-review-and-task-merge/sections/01-task-merge.md)
- B 命名与协议：[sections/02-review-board.md](2026-08-19-slash-review-and-task-merge/sections/02-review-board.md)

# Final Arbitration

Arbiter: Grok 4.6 会审
Time: 2026-08-19

Final verdict: **No-Go for 提交 / 推送 / 发布。** 用户斜杠可继续用，但当前文本有成立的 P1。

用户本轮把「没问题就提交推送并发布」写进 `/dyro-review-board` 参数。会审 Go 也不构成该授权。`next.commands` 为空，不发明交付命令。

## Independent checks by arbiter

1. `explain_task` / `_explain_with_config` 只解释调度（backlog/assigned、依赖、decision、conflict）。源码无 review / receipt / signoff / `PROOF_DECAYED`。A-02 成立。
2. `_require_yes`：无 `--yes` 且非 `--dry-run` 才拒绝。官方只读预检是 `dyro --dry-run task merge <id>`，不是 `task explain`。
3. `cmd_next` 在 ready / needs_repair 都不发出 `task merge`。本 skill 打印 `--yes` 是自制 handoff，不是 `next.commands`。
4. `merge_task` 仍是真正的门：status、复核绑定、signoff、线分支、线干净。预检漏了，CLI 仍会拒。因此 A-01/A-02 不定为「会立刻写坏 Git」的 P0，定为 **P1（预检会撒谎）**。
5. 包装层 L15 把禁词扩到无范围的 `board`，与 `/dyro-review-board`、内部 id、恢复命令、记录文件名冲突。用户只确认禁「座位」。F3 冲突成立。
6. 包装层 sibling 路径按 CWD 读会找不到已安装协议。F1 成立。
7. 同一句「没问题就发布」：包装层禁令带「因为会审结果」，挡不住本轮用户授权误读。本仲裁用拒绝发布来执行闸门，并定为 P1。

## P0

无。没有已证实的「本 skill 进程会自己 merge / push / 发布」路径。

评审员 A 的 A-01/A-02、B 的 F5 原标 P0。仲裁降为 P1：爆炸半径是误导 handoff 和漏预检，真正写 Git 仍要人跑 `--yes`，且 `merge_task` 仍执法。

## P1（采纳）

1. **`task explain` 不能证明 merge 就绪**（A-02，采纳）。预检第 5 步是死条件。应改用 `--dry-run task merge`，禁止 `--yes`。
2. **预检 allowlist 看不到复核 / signoff / 任务 HEAD / allow_push**（A-03，采纳）。与第 1 条同一修法。
3. **把自制 `task merge --yes` 写成控制面 `User action`**（A-01/A-05，降为 P1）。必须标明「不是 `next.commands`」，且本 skill 不得执行 `--yes`。
4. **口头 push 就拼 `--push`**（A-04，采纳）。默认不要 `--push`。
5. **sibling `dyro-board` 路径不稳定**（F1，采纳）。写死查找顺序：本 skill 的父目录 / 已安装 `dyro-board`。
6. **禁词 `board` 过宽**（F3，采纳）。对人只禁「座位」。命令名、内部 id、记录文件名可说。
7. **同一轮会审参数里的提交/推送/发布没有硬闸**（F5，降为 P1）。包装层必须写：同一句交付请求一律拒绝，拆成另一次命令。
8. **双触发 / 包装层可当残缺协议跑**（F2/F4，采纳）。包装层加 `disable-model-invocation: true`；未读到 first-party 协议则停。

## P2

- A-06 explain 停条件歧义（第 1 条修掉后减弱）
- A-07 `task status` 双模态
- A-08/A-09 合入主线与单参数歧义
- A-10 doctor FAIL 范围
- F6 Codex yaml 弱于 SKILL.md
- A-11 / A-12 须人工核

## Go / No-Go

| 对象 | 结论 |
| --- | --- |
| 当作用户斜杠继续用 | Conditional Go（先收下 P1 文本） |
| 提交 / 推送 / 发布这些 skill 或任何 Dyro/Huiyichu 仓 | **No-Go** |
| 用本会审代替 `task review` / 发版 | **No-Go** |

## 须人工核

1. 非 Grok 宿主是否尊重 `disable-model-invocation`。
2. agent 会不会因「不要说 board」而拒绝念出 `/dyro-review-board`（修文案后应消失）。
3. `dev_0814` 线分支漂移是否要另修；本会审不处理。

## Next human Dyro command

无。`dyro --workspace dyro-engineering next`：`mutation_available=false`，`commands=[]`。不发明 merge / push / publish。
