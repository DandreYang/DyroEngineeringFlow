# Local Agent Dispatch (ADR-0002)

First-party **local multi-agent dispatch** (shipped with the `dyro` wheel).
Entry points: `dyro dispatch …` or `python -m experiments.local_agent_dispatch …`.
Never merges, pushes, or signs off — delivery still uses Dyro Core gates/review/merge.

Design: [`docs/designs/optional-local-agent-dispatch.md`](../../docs/designs/optional-local-agent-dispatch.md)  
Discipline: [`docs/agent-orchestration-discipline.md`](../../docs/agent-orchestration-discipline.md)

## Stages

| Level | Capability |
| --- | --- |
| L0 | TaskContract, ContextGuard, LocatorVerify, process identity |
| L1 | RunStore, dual-scope slot leases, strict shadow integration |
| L2 | `echo` / `codex` / `claude` adapters + CLI `run` / `result` |
| L3 | `panel`, skill render, routes, `gc` |
| L4 | `stage5-bridge` dry-run for external-workflow evidence packs |

## CLI

```sh
# After pipx/pip install dyro (or from a repo checkout):
dyro dispatch doctor
dyro dispatch backends

dyro dispatch run --project . --file task.json --wait --backend echo
dyro dispatch result <run_id>
dyro dispatch panel --project . --file task.json --members echo
dyro dispatch skill-render --write
dyro dispatch gc --dry-run
dyro dispatch stage5-bridge /path/to/evidence-pack

# Equivalent module form:
python3 -m experiments.local_agent_dispatch doctor
```

State home: `~/.dyro/local-agent-dispatch/` (override with `--home` or `DYRO_LOCAL_AGENT_DISPATCH_HOME`).

## Task JSON shape

See design §4 (five-part contract). Backend `echo` is always available for offline dry runs.

## Tests

```sh
python3 -m unittest tests.test_local_agent_dispatch tests.test_local_agent_dispatch_l1_l4 -v
```
