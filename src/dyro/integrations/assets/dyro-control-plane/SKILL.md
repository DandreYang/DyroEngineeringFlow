---
name: dyro-control-plane
description: Inspect a Dyro workspace and prepare bounded read-only plans from Codex. Use when a request asks Codex to discover registered Dyro workspaces, inspect Bridge capabilities or workspace state, or explain and plan an existing Dyro Objective without executing delivery operations.
---

# Dyro Control Plane

Treat Dyro as the delivery control plane. Observe facts and prepare a plan; leave every state-changing action to the user in Dyro.

## Workflow

1. Observe before planning.
   - From any directory, run `dyro workspace list` to discover registered workspaces.
   - Use `dyro --workspace <alias> status` for a human-readable, read-only view.
   - For typed automation, invoke the installed `dyro-bridge` once per JSON request. Start with `bridge.hello`, then `bridge.capabilities.compact`, and fetch `bridge.operation.schema` only for the operation needed.
2. Inspect one workspace.
   - Prefer `workspace.resolve` before `workspace.observe`.
   - Supply an explicit workspace alias when multiple workspaces exist.
   - Treat partial or unavailable observations as unknown, never as ready.
3. Plan without executing.
   - Use `objective.plan` only for an existing Objective ID.
   - Present the returned plan, warnings, blockers, and confirmation digest to the user.
   - Ask the user to return to Dyro to approve and execute any next action.

## Bridge Request Shape

Send exactly one UTF-8 JSON object to `dyro-bridge` and close stdin:

```json
{"protocol":{"major":1,"minor":0},"client":{"name":"codex-integration","version":"1"},"operation":"bridge.capabilities.compact","input":{}}
```

For `objective.plan`, use `{"objective_id":"<ID>"}` as `input`. Check `ok`, structured errors, completeness, warnings, and capability availability before relying on `data`.

## Safety Boundary

- Do not run or imitate `dispatch`, `objective apply`, task execution, merge, push, release, or publish.
- Do not edit Dyro state files or manufacture approval/confirmation fields.
- Do not infer final readiness from summaries, missing integration inspection, or partial data.
- If the requested operation is unavailable, explain the limitation and give the exact read-only Dyro command the user can run next.
- End with a concise observation and plan, then identify the user-controlled Dyro action required to continue.
