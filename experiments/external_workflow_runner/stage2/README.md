# External Workflow Runner Stage 2

Stage 2 builds on Stage 1 with:

1. **Provider modes** — `fake` and `simulated-cli` (raw vendor-like output only on Broker tmpfs)
2. **Raw-output destruction** — raw files under `/tmp/provider-raw` are deleted before reply; residue fails shutdown
3. **Mid-run claim renewal** — Supervisor-only renewal loop while the workflow holds
4. **IPC protocol v1/v2** — v2 adds optional `schema_hint`; unsupported versions fail closed
5. **Still no** Dyro evidence / review / signoff / merge / push

## Verification

```sh
docker pull oven/bun:1.3.11-slim
python -m unittest tests.test_external_workflow_runner_stage2
python -m unittest
```

See [`STAGE2_REPORT.md`](STAGE2_REPORT.md).
