# Dyro Audit Witness Protocol

The audit witness protocol anchors the local trust audit in an independently
operated append-only service. It makes deletion, rewriting, rollback, and
forking detectable across the control plane and witness boundary.

## Roles

- The Dyro control plane signs outbound batches with the `audit-export`
  signature domain.
- The witness validates the client key and independently replays every event
  from its durable sequence and chain head.
- The witness persists accepted batches in WORM or equivalent append-only
  storage and signs receipts with the `audit-receipt` domain.
- The control plane trusts the witness receipt public key locally and stores
  the latest verified receipt under `.dyro/audit-witnesses/`.

Client export keys and witness receipt keys are separate roles. A key from one
domain must never be accepted in the other domain.

## Hash chain

The genesis head is 32 zero bytes. For each one-based local event sequence:

```text
head_n = SHA-256(head_n-1 || RFC8785({"sequence": n, "event": event_n}))
```

The control plane recomputes the already witnessed prefix before every sync.
A mismatch fails closed before any append request. Every invocation still sends
an empty signed checkpoint when there are no new events, so deletion of both
the local log and local receipt state cannot silently reset the remote anchor.

## Request

The client sends canonical JSON with:

- `type = "dyro.audit.batch"`
- stable workspace and witness IDs
- a fresh 128-bit random request ID for every newly constructed batch
- a signed `requested_at` timestamp with timezone
- inclusive `from_sequence` and `to_sequence`
- previous and resulting SHA-256 chain heads
- the new sequence-tagged events
- the requested witness receipt key ID and optional recovery key ID
- the monotonic receipt-key epoch
- an Ed25519 `audit-export` signature

`Idempotency-Key` is the SHA-256 of the complete canonical signed batch. The
same batch must produce the same receipt. Before POST, the client atomically
persists the complete signed batch as `pending`; a timeout or lost response
must replay those exact canonical bytes even if newer local events exist.
The Witness may return a cached receipt only while that receipt's resulting
sequence and head still equal its current durable checkpoint. A stale cached
batch must be rejected. Cache eligibility also requires the current receipt
key ID, recovery key ID, and receipt-key epoch to match.

An empty checkpoint uses `from_sequence = current_sequence + 1`,
`to_sequence = current_sequence`, and an empty `events` array.

## Receipt

The witness returns a signed JSON object with:

- `type = "dyro.audit.receipt"`
- the same workspace, witness, sequence range, and resulting head
- `batch_sha256` for the complete signed request
- `accepted_at`
- the receipt key ID
- the recovery key ID copied from the signed batch
- the receipt-key epoch copied from the signed batch
- an Ed25519 `audit-receipt` signature

The witness must reject a batch unless all of these hold:

- schema, type, workspace ID, and witness ID are valid
- `from_sequence` is exactly the next durable sequence
- `to_sequence - previous_sequence == len(events)`
- every event sequence is continuous
- `previous_head_sha256` equals the durable current head
- replaying every event with the hash-chain formula produces `head_sha256`

The Witness atomically stores the canonical batch, new sequence/head, and
receipt only after all checks pass.

The client rejects malformed or timezone-free receipt timestamps. A fresh
`accepted_at` must not predate signed `requested_at` beyond five minutes or
exceed the client receive time by more than five minutes.
For later revocation ordering, the client persists its own `verified_at` after
successful receipt validation. Historical signature windows use the signed
`accepted_at`; whether a revocation happened before or after that accepted
checkpoint uses local `verified_at`, avoiding cross-host clock-skew reversal.

## Receipt key rotation

To rotate a receipt key, trust the new `audit-receipt` key locally, then request
it with `--witness-key-id`. The first new-key receipt must embed a
`dyro.audit.key-transition` record binding the exact batch hash, workspace,
witness, sequence, head, old/new key IDs, and old/new receipt-key epochs. The
transition is signed either
by the still-active old `audit-receipt` key or by a pre-trusted offline
`audit-recovery` key configured with `--witness-recovery-key-id`.

The client switches keys only after validating both the transition and the
new-key receipt. Revoke the old key only after this succeeds.

## Transport and storage requirements

Production endpoints must use HTTPS and clients must reject every HTTP
redirect. HTTP is available only through the explicit testing flag.
Authentication tokens are read from an environment variable and never
accepted as command-line values.

The witness implementation is responsible for durable anti-rollback storage.
Recommended deployments use object lock, WORM retention, or an append-only
transparency service with backups in a separate administrative domain.
