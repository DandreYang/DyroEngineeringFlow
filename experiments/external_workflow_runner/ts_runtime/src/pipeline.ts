/**
 * Sequential pipeline: each step receives the previous output.
 * Fail-fast; never continues after a rejected step.
 */

import { PipelineStepError } from "./errors.ts";
import type { Awaitable } from "./types.ts";

export type PipelineStep<TIn, TOut> = (input: TIn) => Awaitable<TOut>;

/**
 * Compose steps left-to-right. Step 0 receives `initial`.
 */
export async function pipeline<T>(
  initial: T,
  steps: readonly PipelineStep<any, any>[],
): Promise<T> {
  if (!Array.isArray(steps)) {
    throw new PipelineStepError(-1, new TypeError("steps must be an array"));
  }
  let value: unknown = initial;
  for (let index = 0; index < steps.length; index += 1) {
    const step = steps[index];
    if (typeof step !== "function") {
      throw new PipelineStepError(index, new TypeError("step must be a function"));
    }
    try {
      value = await step(value);
    } catch (error) {
      throw new PipelineStepError(index, error);
    }
  }
  return value as T;
}
