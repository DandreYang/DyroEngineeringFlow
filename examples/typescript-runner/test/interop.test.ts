import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  canonicalRecord,
  type JsonRecord,
  signRecord,
  validatePurpose,
} from "../src/protocol.js";


interface InteropFixture {
  purpose: string;
  key_id: string;
  record: JsonRecord;
  canonical_json: string;
  private_key_pem: string;
  expected_signature_base64: string;
}


const fixture = JSON.parse(
  readFileSync(
    new URL("../../fixtures/interop-vector.json", import.meta.url),
    "utf8",
  ),
) as InteropFixture;


test("TypeScript canonical bytes and Ed25519 signature match Python", () => {
  assert.equal(canonicalRecord(fixture.record), fixture.canonical_json);
  const signed = signRecord(
    fixture.record,
    validatePurpose(fixture.purpose),
    fixture.key_id,
    fixture.private_key_pem,
  );
  assert.equal(signed.signature.value, fixture.expected_signature_base64);
  assert.equal(signed.signature.purpose, "execution");
  assert.equal(signed.signature.key_id, "runner-ts-fixture");
});
