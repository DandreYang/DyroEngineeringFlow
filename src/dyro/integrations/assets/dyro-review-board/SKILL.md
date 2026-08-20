---
name: dyro-review-board
description: >
  Run Dyro 会审 / 对抗审查. Use when the user runs /dyro-review-board.
  Advisory record only; not Proof and not task review PASS. Same-turn
  commit, push, or publish in the arguments is refused.
disable-model-invocation: true
user-invocable: true
argument-hint: "[topic]"
metadata:
  short-description: "会审 / 对抗审查；不是交付 Proof"
---

# Dyro 会审 / 对抗审查

对人说 **会审** 或 **对抗审查**。不要说「座位」。

命令名 `/dyro-review-board`、内部 id `dyro-board`、`dyro integration status board`、记录文件名里的 `adversarial-board` 都可以说。

内部 id 仍是 `dyro-board`，不要改，也不要另写一套协议。 Follow
`dyro-board` exactly; do not invent a second protocol. The protocol now
requires baseline + seat lifecycle.

## Same-turn delivery

If this turn also asks to 提交、commit、push、发布、publish、`task merge`、`line merge`、`line spawn`、`line sync`、或「没问题就合入」：

1. Do the 会审 only.
2. Refuse those delivery actions in this turn.
3. Say they need a later, separate command. 会审 Go is not that command.

## Do this

1. Load the first-party protocol before starting. Try, in order:
   - `dyro-board/SKILL.md` next to this skill's parent directory
   - the installed `dyro-board` skill already on this host
2. If none of those can be read, stop. Say 会审协议未安装；下一步是 `dyro integration status board`.
3. Follow `dyro-board` exactly. Do not invent a second protocol. Do not
   start 会审 from this wrapper alone.
4. In chat, call the activity 会审 or 对抗审查. Call the output a 记录.

## Say this to developers

- 这是会审 / 对抗审查：独立反证，最后由一个仲裁收口，不是投票。
- 记录不是 Proof，也不是 `task review` PASS。
- 会审给出 Go，也不等于可以 `task merge`、`line merge`、`line spawn`、`line sync`、push 或发布。
- `task review` 仍走 Core 回执绑定；不要用本次会审替代它。

## Do not

- 不要 merge、push、signoff、`line spawn`、`line merge`、`line sync`、`objective apply`、`dispatch run`、`task run`、git commit、或发布。
- 不要改另一位评审员写过的章节。
- 不要把模型票数升级成事实。
- 不要扫描未安装的个人 skill 目录来代替 first-party 协议。
