---
name: dyro-dispatch
description: Plan and dispatch bounded work to multiple local coding-agent harnesses through Dyro, then collect and reconcile their advisory results. Use when the user explicitly asks to parallelize, delegate, obtain independent reviews, compare multiple harnesses, or split a task across agents; use isolated edit mode only when the user also authorizes project changes.
---

# Dyro Dispatch

Treat `dyro dispatch` as an outbound harness, separate from the read-only
`dyro-control-plane` Skill and from Dyro delivery gates.

## Authorization boundary

- Start Providers only after the user explicitly requests parallel, delegated,
  multi-harness, or independent-agent work. A Skill trigger alone is not consent.
- Treat Provider execution as a local-state, process, and potentially network or
  usage-billed effect even when the delegated task is read-only.
- Use `mode=edit` only when the user also authorizes code changes. Edit runs must
  remain in detached worktrees and may return only a patch reference.
- Never merge, push, commit, signoff, release, publish, import production
  evidence, or represent a dispatch result as a Dyro gate.
- Never enable `echo` as a fallback. It is an explicit offline simulation, not a
  Provider conclusion.

## Workflow

1. Inspect capability without creating dispatch state:

   ```bash
   dyro dispatch --dry-run doctor
   dyro dispatch --dry-run backends
   ```

   Dry-run reports passive installation capability with
   `authentication_probe=not_run`; it never starts a Provider authentication
   CLI and must not be treated as proof of login. Use rows with
   `supported=true`, `available=true`, and `execution_kind=provider` only as
   planning candidates. Supported Provider IDs are `codex`, `claude`,
   `cursor-agent`, `opencode`, `grok`, `hermes`, `kimi`, `dsh`, and `pi`; a
   locally installed command may still be not ready. Cursor additionally requires
   `CURSOR_API_KEY` so dispatch can isolate its home from user MCP processes. If
   no Provider qualifies, stop and report the missing capability. Cursor is
   read-only here; do not select it as an edit writer. Kimi binds one selected
   Provider/model route; file-backed OAuth copies only that selected token into
   a per-run home, while keyring-backed OAuth fails closed. DSH uses the reviewed
   `deepseek-official/deepseek-v4-flash` headless route.

2. Choose the smallest useful strategy:

   - Independent opinions on one question: use `panel`.
   - An explicitly requested full-harness comparison: use `panel --members all`;
     it selects every ready Provider and executes at most four concurrently.
   - Different roles or repository slices: use a persistent Batch V1 request.
     It supports two to four independent members and at most one edit writer.
   - One delegated task: use one asynchronous `run`.
   - Review: prefer a finder plus an adversarial verifier.
   - Edit: use exactly one writer for an overlapping repository/fileset, followed
     by an independent verifier.
   - Keep the default fan-out at two or three runs and never exceed the dispatch
     global limit.

3. Build one self-contained TaskContract per role. Include `schema_version=1`,
   an automatically selected backend candidate, `mode`, `strict`, the applicable
   acknowledgement flags, a minimal `files` list, and all five task fields:
   `briefing`, `locations`, `objective`, `constraints`, and `output_contract`.
   Never use `**/*`, inject conversation history, or include credentials and
   local secret files.

   A single delegated role keeps the existing asynchronous run lifecycle:

   ```bash
   dyro dispatch run --project <absolute-project-root> --stdin
   dyro dispatch result <run-id>
   ```

4. For multiple different roles, place two to four member contracts in one
   Batch request with a unique `request_id`, `strategy=independent`, unique
   `role_id` values, and finite `timeout_seconds`. Plan it before creating state:

   ```bash
   dyro dispatch batch-plan --project <absolute-project-root> --stdin
   ```

   Review `effects`, resolved Providers, their non-secret execution profiles,
   context digests, and any edit `base_head`. Planning creates no dispatch state
   and starts no Provider or authentication CLI; it can therefore select an
   installed-but-logged-out candidate. Fail closed on an invalid contract, zero
   matched files, unavailable/capability-incompatible Provider, missing isolation
   acknowledgement, or secret guard finding. Do not weaken the contract to force
   acceptance.

5. Only after that review, start the exact plan by binding its digest:

   ```bash
   dyro dispatch batch-start --project <absolute-project-root> --stdin \
     --expect-plan-sha256 <reviewed-plan-sha256>
   ```

   At this already-authorized execution boundary, run `dyro dispatch backends`
   and require `authenticated=true` for every selected Provider. Start is
   idempotent for the same request ID and plan. It also performs active
   authentication preflight before creating state. Context, Provider execution
   profile, or edit HEAD drift changes the digest and must be replanned. All
   members are preflighted and persisted before any Provider starts.

   For a same-task comparison, use `dyro dispatch panel` only after recognizing
   that current panels duplicate one contract across Providers and wait for the
   whole panel. Do not use `--members all` unless the user explicitly asks for a
   full-harness run and accepts its Provider usage/cost. Batch V1 is not a DAG or
   an asynchronous `all` queue; do not claim dependencies, automatic retries, or
   automatic judging.

6. Retain the returned `orchestration_id`; use the persistent lifecycle instead
   of manually tracking member run IDs:

   ```bash
   dyro dispatch batch-status <orchestration-id>
   dyro dispatch batch-result <orchestration-id> [--wait --timeout 300]
   dyro dispatch batch-cancel <orchestration-id>
   ```

   Do not ingest raw event logs. Preserve `summary`, `confidence`, verified
   evidence, warnings, and `patch_ref`. Cancellation is cooperative: a running
   member becomes cancelled only after its exact worker generation observes the
   request and backend cleanup is proven. An unprovable cleanup remains
   `running`/`attention_required`; never report a false cancellation. There is no
   retry or resume command in Batch V1.

   GC retains a small request tombstone. Reusing a garbage-collected
   `request_id` is rejected so a retry cannot silently start a second billed
   batch; use a new request ID only for an intentionally new execution.

   Cancellation and timeout supervise Dyro's dedicated POSIX process group, not
   an OS container. A deliberately daemonized child that escapes that group is
   outside this guarantee; all shipped real Providers therefore remain marked
   unconfined and require explicit acknowledgement.

7. Verify claims against real artifacts. A verifier must use a different method
   from the finder where possible. Preserve disagreements and unknowns; never use
   majority vote as approval.

## Handoff

Report the selected roles and Providers, run IDs and terminal states, verified
evidence, disagreements, unknowns, and any patch reference. Clearly state that
results are advisory and that normal Dyro review, gates, signoff, merge, and push
boundaries still apply.
