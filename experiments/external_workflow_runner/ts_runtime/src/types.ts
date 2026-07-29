/**
 * Shared types for Dyro semantic-flow (first-party, isolation-oriented).
 */

/** Value or thenable. */
export type Awaitable<T> = T | PromiseLike<T>;

/** Lazy task invoked by flow combinators. */
export type FlowTask<T> = () => Awaitable<T>;

/** How parallel combinators treat task failures. */
export type ParallelFailureMode =
  /** Any rejection fails the whole group (default, fail-closed). */
  | "fail-closed"
  /** Collect successes and structured failures; never silent null. */
  | "collect";

export type ParallelOptions = {
  /** Max concurrent tasks. Default: unlimited within the group. */
  concurrency?: number;
  /** Fail-closed (default) or collect partial results. */
  failureMode?: ParallelFailureMode;
  /** Optional wall-clock deadline for the entire group in milliseconds. */
  deadlineMs?: number;
};

export type SettledTaskResult<T> =
  | { status: "fulfilled"; value: T }
  | { status: "rejected"; error: unknown };
