/**
 * Agent contract for Dyro semantic-flow.
 *
 * Intentionally has NO default vendor runtime and does NOT inherit process.env
 * for credentials. Callers must inject a Broker-backed Agent.
 */

import type { Awaitable } from "./types.ts";

export type AgentOptions = {
  cwd?: string;
  model?: string;
  /** Opaque schema hint for structured providers. */
  schemaHint?: string;
  signal?: AbortSignal;
};

/**
 * Isolation-friendly agent loop contract.
 */
export interface Agent {
  run<TOutput = string>(
    prompt: string,
    options?: AgentOptions,
  ): Awaitable<TOutput>;
}

export type AgentFunction = <TOutput = string>(
  prompt: string,
  options?: AgentOptions,
) => Promise<TOutput>;

/**
 * Bind an injected Agent to a callable. Refuses null/undefined runtimes.
 */
export function bindAgent(runtime: Agent): AgentFunction {
  if (!runtime || typeof runtime.run !== "function") {
    throw new TypeError(
      "bindAgent requires an injected Agent with a run() method; no default vendor agent exists",
    );
  }
  return async <TOutput = string>(
    prompt: string,
    options?: AgentOptions,
  ): Promise<TOutput> => {
    if (!prompt || typeof prompt !== "string") {
      throw new TypeError("agent prompt must be a non-empty string");
    }
    return runtime.run<TOutput>(prompt, options);
  };
}
