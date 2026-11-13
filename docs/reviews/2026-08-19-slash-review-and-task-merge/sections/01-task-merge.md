# Reviewer A — 安全与契约

Reviewer: independent critic
Object: `dyro-task-merge`

Findings A-01 … A-12 as signed in the 2026-08-19 会审 run. Not rewritten.

成立的高优先级：

- A-01：预检通过后打印 `task merge --yes`，而 `next` 从不发出该命令。
- A-02：`task explain` 不报告 review / receipt / signoff / PROOF_DECAYED。
- A-03：allowlist 观察不到 merge 谓词。
- A-04：用户说 push 就发明 `--push`。
- A-05：借用控制面 `User action` 外形。
- A-06：explain 停条件歧义。

P2 / 须人工核：A-07 … A-12。
