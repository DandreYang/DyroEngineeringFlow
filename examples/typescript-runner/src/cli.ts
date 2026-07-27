#!/usr/bin/env node

import { lstatSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  type JsonRecord,
  signRecord,
  validatePurpose,
} from "./protocol.js";


interface Options {
  input: string;
  output: string;
  privateKey: string;
  purpose: string;
  keyId: string;
}


function usage(): never {
  throw new Error(
    "usage: dyro-ts-sign --input provenance.json --output signed.json " +
    "--private-key runner.pem --purpose execution --key-id runner-2026",
  );
}


function parseOptions(argv: string[]): Options {
  const values = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name?.startsWith("--") || value === undefined) {
      usage();
    }
    if (values.has(name)) {
      throw new Error(`duplicate option: ${name}`);
    }
    values.set(name, value);
  }
  const required = ["--input", "--output", "--private-key", "--purpose", "--key-id"];
  if (values.size !== required.length || required.some((name) => !values.has(name))) {
    usage();
  }
  return {
    input: values.get("--input")!,
    output: values.get("--output")!,
    privateKey: values.get("--private-key")!,
    purpose: values.get("--purpose")!,
    keyId: values.get("--key-id")!,
  };
}


function readPrivateKey(path: string): string {
  const target = resolve(path);
  const metadata = lstatSync(target);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    throw new Error(`private key must be a regular file, not a symlink: ${target}`);
  }
  if (process.platform !== "win32" && (metadata.mode & 0o077) !== 0) {
    throw new Error(`private key permissions must be 0600: ${target}`);
  }
  return readFileSync(target, "utf8");
}


function readRecord(path: string): JsonRecord {
  const value: unknown = JSON.parse(readFileSync(resolve(path), "utf8"));
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    throw new Error("input JSON must be an object");
  }
  return value as JsonRecord;
}


function main(): void {
  const options = parseOptions(process.argv.slice(2));
  const purpose = validatePurpose(options.purpose);
  const signed = signRecord(
    readRecord(options.input),
    purpose,
    options.keyId,
    readPrivateKey(options.privateKey),
  );
  writeFileSync(
    resolve(options.output),
    `${JSON.stringify(signed, null, 2)}\n`,
    { encoding: "utf8", flag: "wx", mode: 0o600 },
  );
}


try {
  main();
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`dyro-ts-sign: ${message}\n`);
  process.exitCode = 1;
}
