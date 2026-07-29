/**
 * Named phase wrapper for timing and structured logs.
 * Phases never swallow errors.
 */

import type { Awaitable } from "./types.ts";

export type PhaseMeta = {
  name: string;
  startedAt: number;
  endedAt: number;
  durationMs: number;
  ok: boolean;
};

export type PhaseLogger = (event: {
  kind: "phase_start" | "phase_end";
  name: string;
  durationMs?: number;
  ok?: boolean;
}) => void;

/**
 * Execute `body` as a named phase. Errors propagate after phase_end(ok=false).
 */
export async function phase<T>(
  name: string,
  body: () => Awaitable<T>,
  logger?: PhaseLogger,
): Promise<{ value: T; meta: PhaseMeta }> {
  if (!name || typeof name !== "string") {
    throw new TypeError("phase name must be a non-empty string");
  }
  if (typeof body !== "function") {
    throw new TypeError("phase body must be a function");
  }
  const startedAt = Date.now();
  logger?.({ kind: "phase_start", name });
  try {
    const value = await body();
    const endedAt = Date.now();
    const meta: PhaseMeta = {
      name,
      startedAt,
      endedAt,
      durationMs: endedAt - startedAt,
      ok: true,
    };
    logger?.({
      kind: "phase_end",
      name,
      durationMs: meta.durationMs,
      ok: true,
    });
    return { value, meta };
  } catch (error) {
    const endedAt = Date.now();
    logger?.({
      kind: "phase_end",
      name,
      durationMs: endedAt - startedAt,
      ok: false,
    });
    throw error;
  }
}
