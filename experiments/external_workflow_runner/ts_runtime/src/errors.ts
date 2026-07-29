/**
 * Structured errors for Dyro semantic-flow.
 * Failures are explicit; combinators do not convert errors into null.
 */

export class SemanticFlowError extends Error {
  readonly code: string;

  constructor(code: string, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "SemanticFlowError";
    this.code = code;
  }
}

export class ParallelGroupError extends SemanticFlowError {
  readonly failures: ReadonlyArray<{ index: number; error: unknown }>;

  constructor(
    failures: ReadonlyArray<{ index: number; error: unknown }>,
    message = "One or more parallel tasks failed",
  ) {
    super("parallel_group_failed", message);
    this.name = "ParallelGroupError";
    this.failures = failures;
  }
}

export class PipelineStepError extends SemanticFlowError {
  readonly stepIndex: number;

  constructor(stepIndex: number, cause: unknown) {
    super(
      "pipeline_step_failed",
      `Pipeline step ${stepIndex} failed`,
      cause instanceof Error ? { cause } : undefined,
    );
    this.name = "PipelineStepError";
    this.stepIndex = stepIndex;
  }
}

export class FlowDeadlineError extends SemanticFlowError {
  constructor(deadlineMs: number) {
    super(
      "flow_deadline_exceeded",
      `Flow group exceeded deadline of ${deadlineMs}ms`,
    );
    this.name = "FlowDeadlineError";
  }
}
