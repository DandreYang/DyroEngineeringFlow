# Dyro TypeScript Runner Reference

This example implements Dyro's cross-language cryptographic boundary:

- RFC 8785 JSON Canonicalization Scheme via `json-canonicalize`
- `dyro/<purpose>/v1\0` signature domains
- Ed25519 signing with Node.js `crypto.sign(null, ...)`
- the same key ID and signature envelope consumed by the Python control plane

It deliberately does not execute arbitrary gates. A production runner should
construct and validate its provenance record before invoking this signer.

## Install and test

```bash
npm ci
npm test
```

## Sign a provenance record

The private key must be a regular file with mode `0600`; the output path must
not already exist.

```bash
npm run build
node dist/src/cli.js \
  --input provenance.json \
  --output signed-provenance.json \
  --private-key /secure/runner.pem \
  --purpose execution \
  --key-id runner-2026
```

The fixture key under `fixtures/` is test-only and must never be trusted or
used outside interoperability tests.
