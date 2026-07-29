# @dyro/semantic-flow

First-party TypeScript semantic-flow primitives for Dyro's **optional external**
workflow sandbox experiment.

## Design goals (vs ad-hoc third-party agent workflow runtimes)

| Concern | This runtime |
| --- | --- |
| Parallel failure | Fail-closed by default; never silent `null` |
| Agent defaults | None — Broker-backed Agent must be injected |
| Credentials | Not read from `process.env` by the runtime |
| Concurrency | Explicit `concurrency` + optional `deadlineMs` |
| Product boundary | Not Dyro Core; vendored into experiment bundles only |

## API

- `parallel(tasks, options?)` / `parallelSettled(tasks, options?)`
- `pipeline(initial, steps)`
- `phase(name, body, logger?)`
- `bindAgent(agent)`

## Packaging

Stage assemblers copy this tree into each sandbox bundle as
`vendor/dyro-semantic-flow/` and bind it via content hash in the runtime lock.
