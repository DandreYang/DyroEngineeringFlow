/**
 * @dyro/semantic-flow — first-party task-local semantic flow primitives.
 *
 * Designed for Dyro external sandbox bundles:
 * - fail-closed parallel by default
 * - no default vendor Agent / no credential env inheritance
 * - explicit concurrency and deadlines
 */

export { bindAgent } from "./agent.ts";
export type { Agent, AgentFunction, AgentOptions } from "./agent.ts";
export {
  FlowDeadlineError,
  ParallelGroupError,
  PipelineStepError,
  SemanticFlowError,
} from "./errors.ts";
export { parallel, parallelSettled } from "./parallel.ts";
export { phase } from "./phase.ts";
export type { PhaseLogger, PhaseMeta } from "./phase.ts";
export { pipeline } from "./pipeline.ts";
export type { PipelineStep } from "./pipeline.ts";
export type {
  Awaitable,
  FlowTask,
  ParallelFailureMode,
  ParallelOptions,
  SettledTaskResult,
} from "./types.ts";
