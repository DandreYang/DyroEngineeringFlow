# External Workflow Runner Stage 1

Stage 1 packages the first-party `@dyro/semantic-flow` runtime into a fixed
sandbox bundle, starts an isolated Broker with a fake provider, binds canonical
input, and enforces the execution-key cleanup gate.

## What Stage 1 proves

1. **First-party runtime identity** — content-hash of `ts_runtime/` locked in `runtime-lock.json`
2. **Fixed bundle** — workflow + broker + vendored `dyro-semantic-flow`
3. **Isolated Broker IPC** — Docker internal network + loopback TCP
4. **Fake provider** before real credentials
5. **Canonical input** + sanitized telemetry
6. **Claim renewal design** + no execution key until cleanup
7. **No Dyro evidence/signoff**

See [`STAGE1_REPORT.md`](STAGE1_REPORT.md).
