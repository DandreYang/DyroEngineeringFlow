---
name: dyro-board
description: Run Dyro's first-party adversarial review-board protocol. Use when the user asks for multi-model review, 会审, 对抗, 能不能发, Go/No-Go, or P0/P1 arbitration. Auto-load this board seat. Findings are advisory records, not Proof and not a task review PASS.
---

# Dyro Review Board

Run the first-party adversarial record protocol. Auto-load this seat when the
user asks for a board, 会审, 对抗, release Go/No-Go, or P0/P1 arbitration. Do
not wait to be named. Loading the seat is not merge, signoff, or Proof.

## Auto-trigger

Load immediately when the user asks to:

- run a review board, multi-model review, or independent opinions
- 会审, 对抗, 能不能发
- produce P0/P1/P2 or Go/No-Go

Do not load this seat to write product code. That is `dyro-executor`. Do not
load it for a receipt-bound `task review`; that Core command still owns PASS.
Do not scan personal host skill directories.

## Record protocol

1. One shared review file. Prefer
   `docs/reviews/YYYY-MM-DD-<topic>-adversarial-board.md`.
2. Each reviewer writes only in a signed section. Do not edit, rewrite, or
   summarize another section.
3. Source code and live contracts outrank plans and prior reviews.
4. Unprovable claims are `须人工核`.
5. Final arbitration deduplicates, resolves conflicts, and emits P0/P1/P2,
   Go/No-Go, and the next human Dyro command if one already exists.

## Authority

- Board output is a record. It is not a Proof, gate, or `task review` PASS.
- Do not merge, push, signoff, `objective apply`, `dispatch run`, or
  `task run` because the board said Go.
- Do not promote model votes, screenshots, or summaries into delivery truth.
- If Dyro `next.commands` is empty, do not manufacture a mutation.

## Optional orientation

When the user already named a workspace alias, a read-only status check is
allowed:

```bash
dyro --workspace <alias> next --format json
```

## Handoff

Keep the shared file as the record. In chat, state the verdict, the P0/P1
list, unknowns marked `须人工核`, and that delivery still uses Dyro gates
and review. Omit local paths the user did not supply.
