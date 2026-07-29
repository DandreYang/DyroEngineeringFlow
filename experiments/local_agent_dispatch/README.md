# Local Agent Dispatch (ADR-0002, L0)

Removable first-party experiment implementing the **contract layer** of optional
local multi-agent dispatch:

- five-part `TaskContract`
- path + content `ContextGuard`
- strict shadow materialization
- evidence locator verification
- process identity helpers for future leases

Not installed with the `dyro` package. Does **not** call signoff/merge/push.
Does **not** depend on third-party collaboration products.

Design: [`docs/designs/optional-local-agent-dispatch.md`](../../docs/designs/optional-local-agent-dispatch.md)

## Tests

```sh
python3 -m unittest tests.test_local_agent_dispatch -v
```
