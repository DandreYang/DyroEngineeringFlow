---
name: dyro-agent-bridge
description: Inspect and plan Dyro state through the source-only Agent Bridge public process. Use for workspace discovery, bounded observations, and non-executable Objective plans. Never use for execution, apply, dispatch, or delivery mutations.
---

# Dyro Agent Bridge

Treat Agent Bridge as a one-shot inspect-and-plan process. It is not an
authorization boundary and is not `dispatch`.

This Skill is source-tree only in the `0.7.x` train. Do not install it into a
host discovery directory, and do not invent a `dyro-bridge` console script.

## Process

If `python -m dyro.bridge` cannot be imported, stop. The published wheel is
bridge-free; that is unsupported, not a reason to use a write-capable command.

Each call is one UTF-8 JSON object on stdin, then close stdin. Accept exactly
one JSON object and one newline on stdout. Empty stderr is required. Exit `4`
or `error.code=OPERATION_UNAVAILABLE` means this host has no public Bridge
surface—report unsupported and stop. Do not retry with `dyro`, `dispatch`,
`apply`, or a guessed CLI flag.

## Routing

1. First call `bridge.capabilities.compact`.
2. Fetch exactly one `bridge.operation.schema` for the chosen operation.
3. Call only that operation if compact lists it `public_available`.

Allowlisted operations after a public compact:

- `workspace.resolve`
- `workspace.list`
- `workspace.observe`
- `objective.plan`

`implemented_testable` and `declared` operations stay unavailable through this
process. `line.list`, `task.list`, explain, graph, tick, and attention are not
public in Phase 0.

Positive triggers: workspace identity, inventory, observation, or a
non-executable Objective plan.

Negative triggers: apply, dispatch, task run, gates, merge, push, sign-off,
release, publish, console, install, or any confirmation/approval field.

## Request

```json
{
  "protocol": {"major": 1, "minor": 0},
  "client": {"name": "dyro-agent-bridge-skill", "version": "0.7.6"},
  "operation": "bridge.capabilities.compact",
  "input": {}
}
```

Required fields are `protocol.major`, `client.name`, `client.version`,
`operation`, and `input`. Do not send `actor`, `approval`, `confirmation`,
`command`, `argv`, `shell`, `apply`, or `dry_run`.

## Response

Keep evidence separate from judgment. `ok=true` data is observed. A plan with
`executable=false` and `authorization=none` is not permission to act. Absolute
paths, argv, and logs must not be repeated.

## Hard safety boundary

- Do not run `dyro console`, `dyro dispatch`, `objective apply`, `task run`,
  `task gates`, merge, push, or integration install.
- Do not edit project or Dyro state files.
- Do not treat a plan digest as approval.
- End after observation and planning.
