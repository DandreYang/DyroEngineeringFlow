import { createPrivateKey, sign as cryptoSign } from "node:crypto";

import { canonicalize } from "json-canonicalize";


export const SIGNATURE_ALGORITHM = "ed25519";
export const SIGNATURE_PURPOSES = ["execution", "review", "signoff"] as const;

export type SignaturePurpose = (typeof SIGNATURE_PURPOSES)[number];
export type JsonPrimitive = null | boolean | number | string;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonRecord = { [key: string]: JsonValue };

export interface SignedRecord extends JsonRecord {
  signature: {
    schema_version: 1;
    algorithm: typeof SIGNATURE_ALGORITHM;
    purpose: SignaturePurpose;
    key_id: string;
    value: string;
  };
}

const KEY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;


export function validatePurpose(value: string): SignaturePurpose {
  if (!SIGNATURE_PURPOSES.includes(value as SignaturePurpose)) {
    throw new Error(`signature purpose must be one of: ${SIGNATURE_PURPOSES.join(", ")}`);
  }
  return value as SignaturePurpose;
}


export function validateKeyId(value: string): string {
  if (!KEY_ID_PATTERN.test(value)) {
    throw new Error("key ID must contain only letters, numbers, dots, underscores, and hyphens");
  }
  return value;
}


export function canonicalRecord(record: JsonRecord): string {
  const unsigned = { ...record };
  delete unsigned.signature;
  return canonicalize(unsigned);
}


export function signingMessage(record: JsonRecord, purpose: SignaturePurpose): Buffer {
  const domain = Buffer.from(`dyro/${purpose}/v1\0`, "ascii");
  return Buffer.concat([domain, Buffer.from(canonicalRecord(record), "utf8")]);
}


export function signRecord(
  record: JsonRecord,
  purpose: SignaturePurpose,
  keyId: string,
  privateKeyPem: string,
): SignedRecord {
  if (Object.hasOwn(record, "signature")) {
    throw new Error("record already contains signature");
  }
  const normalizedKeyId = validateKeyId(keyId);
  const signature = cryptoSign(
    null,
    signingMessage(record, purpose),
    createPrivateKey(privateKeyPem),
  );
  return {
    ...record,
    signature: {
      schema_version: 1,
      algorithm: SIGNATURE_ALGORITHM,
      purpose,
      key_id: normalizedKeyId,
      value: signature.toString("base64"),
    },
  };
}
