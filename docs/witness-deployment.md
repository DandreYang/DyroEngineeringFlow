# Deploying a Dyro Audit Witness

`dyro witness serve` is a small, stateful service for the audit Witness
protocol. It verifies `audit-export` batches, independently replays the hash
chain, writes a create-only batch/receipt record, and atomically advances the
workspace checkpoint only after the record is durable.

It is intentionally not an object-storage client. The mutable checkpoint and
the immutable records have separate roots: `--storage-root` stores only the
current checkpoint, while `--record-archive-root` stores create-only
`records/`. For AWS, place or replicate the record archive to an S3 bucket with Object Lock
enabled before the bucket receives any objects; enable versioning, a
compliance retention policy, restricted delete permissions, and a separate
administrative account. A normal local filesystem is suitable only for
development and integration testing.

## Provisioning

1. Create a dedicated trust root and install each client export public key
   under `.dyro/trust/ed25519/audit-export/` through the same controlled key
   ceremony used by the control plane. A single-tenant Witness must set its
   `--workspace-id`. For a shared Witness, provide one
   `--client-workspace-binding KEY_ID=WORKSPACE_ID` for every trusted client
   export key; an unbound key is denied even when it exists in the trust root.
2. Generate the active `audit-receipt` key outside the storage volume. Mount
   its private PEM as a read-only secret with mode `0600` and ownership
   `10001:10001`, the container's `witness` UID/GID.
3. Set a random bearer token in `DYRO_WITNESS_TOKEN`; keep it out of command
   lines, images, and source control.
4. Expose the service only behind TLS. Either provide `--tls-cert` and
   `--tls-key`, or terminate TLS at a reverse proxy that forwards only to a
   private network listener.
5. Keep `/var/lib/dyro-witness` on durable mutable storage and mount
   `/var/lib/dyro-witness-records` on WORM storage or replicate it to Object
   Lock. Back up both roots outside the service account.

Example:

```bash
DYRO_WITNESS_TOKEN="$(openssl rand -hex 32)" \
dyro witness serve \
  --storage-root /var/lib/dyro-witness \
  --client-trust-root /etc/dyro-witness/trust-root \
  --witness-id primary \
  --receipt-key-id witness-2026 \
  --receipt-signing-key /run/secrets/witness-2026.pem \
  --record-archive-root /var/lib/dyro-witness-records \
  --workspace-id production \
  --expected-endpoint https://audit.example.com/v1/dyro/batches \
  --auth-token-env DYRO_WITNESS_TOKEN \
  --tls-cert /run/secrets/tls.crt \
  --tls-key /run/secrets/tls.key \
  --host 0.0.0.0
```

The endpoint provides `GET /healthz` and accepts only canonical JSON at
`POST /v1/dyro/batches`. It requires `Authorization: Bearer <token>` unless
the explicit local-only `--allow-unauthenticated` flag is used.
The configured read timeout is also an absolute per-connection deadline,
covering TLS negotiation, request headers, and the body. Keep a rate-limiting
reverse proxy in front of public deployments as an additional network control.

Witness rejects symbolic links anywhere beneath either storage root. Give the
service account exclusive write access to those roots; do not place its state
below a path writable by clients or other tenants.

## Receipt key rotation

First install the next receipt public key at every client. Reconfigure the
server with the next `--receipt-key-id` and `--receipt-signing-key`, plus the
old receipt key (or offline recovery key) as all three transition options:
`--transition-key-id`, `--transition-signing-key`, and
`--transition-purpose audit-receipt|audit-recovery`. The server emits the
checkpoint-bound transition and new receipt atomically. Revoke the old client
trust entry only after clients confirm the new receipt.

## Container asset

`deploy/witness/docker-compose.example.yml` starts the service with TLS,
read-only secrets, and a bounded 32-request listener. Its named record volume
is a runnable integration example, not WORM storage; replace it with a volume
or replication workflow backed by Object Lock before production. Bucket
retention, DNS, and certificate issuance remain at the cloud boundary.
