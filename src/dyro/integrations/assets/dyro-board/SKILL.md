---
name: dyro-board
description: Internal Dyro 会审 protocol (not a user slash; humans run /dyro-review-board). Auto-load this board seat for multi-model review, 会审, 对抗, 能不能发, Go/No-Go, or P0/P1 arbitration. Findings are advisory records, not Proof and not a task review PASS.
user-invocable: false
---

# Dyro Review Board

This file is the protocol, not a user slash. Humans run `/dyro-review-board`.

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

0. Before review starts, declare the diff baseline per repo:
   `<repo> <ref> <sha> <date>`. Default the **production line** (usually
   `origin/release`). **Must not default to `master`/`main`.** If
   `master`/`main` has diverged from production, record how many commits.
   If a prior board on the same development line used a different
   baseline, say why.
0b. Search for prior boards on the same development line / same topic. If
    one exists, the record **must** include a section that closes each
    prior P0/P1 as `已闭环` / `未闭环` / `须人工核` with evidence. If the
    search finds none, the record must say `已检索·无先前会审`.
1. One shared review file. Prefer the repo's **existing**
   review-directory convention. If none,
   `docs/reviews/YYYY-MM-DD-<topic>-adversarial-board.md`. If a prior
   board exists on this line, use the same directory. If the repo
   convention and a prior-board directory differ, the prior-board
   directory wins (same line).
2. Each reviewer writes only in a signed section. Do not edit, rewrite, or
   summarize another section.
3. Source code and live contracts outrank plans, prior reviews, **and
   same-batch seat opinions**. The chair must re-check every seat claim
   before it enters final arbitration. Unchecked items must be marked
   `未复核·转述`.
4. Unprovable claims are `须人工核`.
5. Empty `ListAgents` / `TaskOutput` is **not** enough to declare a seat
   dead — those APIs can be empty while the seat is still running. The
   chair must set an explicit wait window (≥ 10 minutes). The clock
   starts at **first dispatch** of the seats, not the first empty poll,
   and **must not publish arbitration** during that window.
6. A seat that misses the window is `逾期未交`, not "lost". Record who
   covers that dimension.
7. Seat reports that arrive after arbitration require a **revised**
   record that lists what changed. No silent merge into the published
   verdict.
8. Any test verdict must record: the full command, the summary line from
   raw output, and whether a pipeline/redirect hid the exit code. Exit
   code or a task-completed notification alone must not assert pass.
   Unrun targets are `未执行` plus why.
9. Final arbitration deduplicates, resolves conflicts, emits P0/P1/P2,
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
