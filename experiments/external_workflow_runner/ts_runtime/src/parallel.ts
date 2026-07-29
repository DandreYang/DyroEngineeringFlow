/**
 * Parallel task group with fail-closed defaults and optional concurrency.
 *
 * Unlike runtimes that map rejections to null and continue, this module
 * defaults to fail-closed: any rejection rejects the whole group with a
 * ParallelGroupError listing every failure.
 */

import { FlowDeadlineError, ParallelGroupError } from "./errors.ts";
import type {
  FlowTask,
  ParallelOptions,
  SettledTaskResult,
} from "./types.ts";

async function runWithConcurrency<T>(
  tasks: readonly FlowTask<T>[],
  concurrency: number,
): Promise<SettledTaskResult<T>[]> {
  const results: SettledTaskResult<T>[] = new Array(tasks.length);
  let next = 0;

  async function worker(): Promise<void> {
    while (true) {
      const index = next;
      next += 1;
      if (index >= tasks.length) {
        return;
      }
      try {
        const value = await tasks[index]!();
        results[index] = { status: "fulfilled", value };
      } catch (error) {
        results[index] = { status: "rejected", error };
      }
    }
  }

  const workers = Array.from(
    { length: Math.min(concurrency, tasks.length) },
    () => worker(),
  );
  await Promise.all(workers);
  return results;
}

/**
 * Run tasks concurrently.
 *
 * @default failureMode fail-closed
 */
export async function parallel<T>(
  tasks: readonly FlowTask<T>[],
  options: ParallelOptions = {},
): Promise<T[]> {
  if (!Array.isArray(tasks) || tasks.length === 0) {
    throw new ParallelGroupError([], "parallel requires a non-empty task list");
  }
  for (const [index, task] of tasks.entries()) {
    if (typeof task !== "function") {
      throw new ParallelGroupError(
        [{ index, error: new TypeError("task must be a function") }],
        "parallel task list contains a non-function entry",
      );
    }
  }

  const concurrency =
    options.concurrency === undefined
      ? tasks.length
      : Math.max(1, Math.floor(options.concurrency));
  if (
    options.concurrency !== undefined &&
    (!Number.isFinite(options.concurrency) || options.concurrency < 1)
  ) {
    throw new ParallelGroupError(
      [],
      "parallel concurrency must be a positive finite number",
    );
  }

  const failureMode = options.failureMode ?? "fail-closed";
  const run = () => runWithConcurrency(tasks, concurrency);

  let settled: SettledTaskResult<T>[];
  if (options.deadlineMs !== undefined) {
    if (
      !Number.isFinite(options.deadlineMs) ||
      options.deadlineMs <= 0
    ) {
      throw new ParallelGroupError([], "parallel deadlineMs must be positive");
    }
    settled = await Promise.race([
      run(),
      new Promise<SettledTaskResult<T>[]>((_, reject) => {
        setTimeout(() => {
          reject(new FlowDeadlineError(options.deadlineMs!));
        }, options.deadlineMs);
      }),
    ]);
  } else {
    settled = await run();
  }

  const failures = settled
    .map((item, index) =>
      item.status === "rejected"
        ? { index, error: item.error }
        : null,
    )
    .filter((item): item is { index: number; error: unknown } => item !== null);

  if (failureMode === "fail-closed" && failures.length > 0) {
    throw new ParallelGroupError(failures);
  }

  if (failureMode === "collect") {
    // Callers must use parallelSettled for structured partial results.
    // collect mode still surfaces a group error if every task failed.
    if (failures.length === tasks.length) {
      throw new ParallelGroupError(failures, "all parallel tasks failed");
    }
  }

  return settled.map((item, index) => {
    if (item.status === "fulfilled") {
      return item.value;
    }
    // collect mode with mixed results: rethrow to avoid silent nulls.
    // Use parallelSettled when partial results are required.
    throw new ParallelGroupError(
      [{ index, error: item.error }],
      "parallel collect mode still refuses silent null results; use parallelSettled",
    );
  });
}

/**
 * Run tasks concurrently and return per-index settled results.
 * Never substitutes null for failures.
 */
export async function parallelSettled<T>(
  tasks: readonly FlowTask<T>[],
  options: Omit<ParallelOptions, "failureMode"> = {},
): Promise<SettledTaskResult<T>[]> {
  if (!Array.isArray(tasks) || tasks.length === 0) {
    throw new ParallelGroupError([], "parallelSettled requires a non-empty task list");
  }
  const concurrency =
    options.concurrency === undefined
      ? tasks.length
      : Math.max(1, Math.floor(options.concurrency));

  const run = () => runWithConcurrency(tasks, concurrency);
  if (options.deadlineMs !== undefined) {
    return Promise.race([
      run(),
      new Promise<SettledTaskResult<T>[]>((_, reject) => {
        setTimeout(() => {
          reject(new FlowDeadlineError(options.deadlineMs!));
        }, options.deadlineMs);
      }),
    ]);
  }
  return run();
}
