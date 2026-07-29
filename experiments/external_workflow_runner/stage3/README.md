# External Workflow Runner Stage 3

Stage 3 adds an **argv-only provider CLI adapter** inside the Broker:

1. Broker spawns a fixed argv fixture (`fake_provider_cli.ts`) with an explicit env allowlist
2. Raw stdout/stderr land only on Broker tmpfs and are destroyed before the IPC reply
3. Provider token (`DYRO_PROVIDER_FAKE_TOKEN`) never enters the Workflow Sandbox
4. Multi-phase claim deadline matrix drives hold + agent + cleanup lease sizing
5. Supervisor mid-run claim renewal continues; evidence/signoff still forbidden

## Verification

```sh
docker pull oven/bun:1.3.11-slim
python -m unittest tests.test_external_workflow_runner_stage3
python -m unittest
```

See [`STAGE3_REPORT.md`](STAGE3_REPORT.md).
