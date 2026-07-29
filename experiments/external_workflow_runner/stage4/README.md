# External Workflow Runner Stage 4

Stage 4 extends Stage 3 with:

1. **Integrity-pinned provider argv** — Broker only spawns allowlisted tokens after verifying the provider script/binary content SHA-256
2. **Dual cleanup verification** — Sandbox *and* Broker containers must be gone before any post-run action
3. **Evidence packing after cleanup** — local sealed pack (manifest + zip); **no** signoff / merge / push
4. **Worktree storage quota** — fail-closed size/file limits on task worktrees

## Verification

```sh
docker pull oven/bun:1.3.11-slim
python -m unittest tests.test_external_workflow_runner_stage4
python -m unittest
```

See [`STAGE4_REPORT.md`](STAGE4_REPORT.md).
