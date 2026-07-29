/**
 * Stand-in provider CLI for Stage 3.
 *
 * Invoked only by the Broker via fixed argv (never shell). Writes raw-looking
 * vendor material to stdout/stderr so Stage 3 can prove capture+destroy on
 * Broker tmpfs. Does not implement a real Codex/Claude integration.
 */
const prompt = process.argv.slice(2).join(" ") || "empty-prompt";
const token = process.env.DYRO_PROVIDER_FAKE_TOKEN ?? "missing-token";

// Raw material must never appear in sanitized Broker responses or Sandbox.
process.stdout.write(
  [
    "BEGIN PRIVATE KEY",
    "RAW_VENDOR_SECRET_MUST_NOT_ESCAPE",
    `token=${token}`,
    `prompt=${prompt}`,
    "sk-stage3-cli-token",
    "final:stage3-cli-summary",
    "",
  ].join("\n"),
);
process.stderr.write(
  ["RAW_VENDOR_STDERR_MARKER", "cli diagnostics only", ""].join("\n"),
);
process.exit(0);
