# External Workflow Runner Stage 5 (PoC closeout)

Stage 5 closes the **local** experiment path:

1. **Host-mounted provider** — allowlisted absolute host path + content SHA pin; bind-mounted **Broker-only**
2. **Evidence dry-run** — validate Stage4-style sealed pack; emit human-review candidate; **no** Core import
3. **Production gate** — ADR-0001 stop conditions encoded as an explicit **NOT_READY** checklist

## Verification

```sh
docker pull oven/bun:1.3.11-slim
python3 -m unittest tests.test_external_workflow_runner_stage5
python3 -m unittest
```

See:

- [`STAGE5_REPORT.md`](STAGE5_REPORT.md)
- [`POC_EVALUATION.md`](POC_EVALUATION.md)
- [`PRODUCTION_NOT_READY.md`](PRODUCTION_NOT_READY.md)
