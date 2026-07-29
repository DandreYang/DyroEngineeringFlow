# Local Agent Dispatch (ADR-0002)

Removable first-party experiment for optional **local multi-agent dispatch**.
Not part of the installed `dyro` package. Never merges, pushes, or signs off.

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
# from repo root
python3 -m experiments.local_agent_dispatch doctor
python3 -m experiments.local_agent_dispatch backends

python3 -m experiments.local_agent_dispatch run \
  --project . --file task.json --wait --backend echo

python3 -m experiments.local_agent_dispatch result <run_id>
python3 -m experiments.local_agent_dispatch panel --project . --file task.json --members echo
python3 -m experiments.local_agent_dispatch skill-render --write
python3 -m experiments.local_agent_dispatch gc --dry-run
python3 -m experiments.local_agent_dispatch stage5-bridge /path/to/evidence-pack
```

State home: `~/.dyro/local-agent-dispatch/` (override with `--home` or `DYRO_LOCAL_AGENT_DISPATCH_HOME`).

## Task JSON shape

See design §4 (five-part contract). Backend `echo` is always available for offline dry runs.

## Tests

```sh
python3 -m unittest tests.test_local_agent_dispatch tests.test_local_agent_dispatch_l1_l4 -v
```
