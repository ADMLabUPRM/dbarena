export interface MatchingRules {
  order_sensitive: boolean;
  numeric_tolerance: number | null;
  null_handling: string;
}

export interface StateCheck {
  sql: string;
  expected_tuples: unknown[][];
  matching?: MatchingRules;
}

export interface VerificationRule {
  sql: string;
  description?: string | null;
}

export interface JudgeCondition {
  expected: string;
  key_points?: string[];
  reward_mode?: "sparse" | "dense";
}

export interface TerminalCondition {
  expected_tuples?: unknown[][];
  matching?: MatchingRules;
  state_checks?: StateCheck[];
  rules?: VerificationRule[];
  judge?: JudgeCondition;
}

export const CATEGORIES = ["OLTP", "OLAP", "ANALYTICS", "RAG"] as const;
export const TASK_TYPES = ["NAV", "IS", "CC", "MIX"] as const;

export interface Task {
  task_id: string;
  goal: string;
  submission: string;
  category: string;
  task_type: string;
  max_steps: number;
  sqlalchemy_url: string;
  terminal_condition: TerminalCondition;
  query_timeout: number;
  row_limit: number | null;
}

export interface TaskPayload {
  task_id: string;
  goal: string;
  submission: string;
  category: string;
  task_type: string;
  max_steps: number;
  sqlalchemy_url: string;
  terminal_condition: TerminalCondition;
  query_timeout: number;
  row_limit: number | null;
}

export interface Observation {
  schema_version: string;
  type: string;
  task: {
    task_id: string;
    goal: string;
    submission: string;
    category: string;
    task_type: string;
  } | null;
  action: { tool: string; arguments: unknown } | null;
  payload: unknown;
  reward: number | null;
  terminated: boolean;
  truncated: boolean;
  step_number: number | null;
  meta: Record<string, unknown>;
}

export interface StepEntry {
  step_number: number;
  action: { tool: string; arguments: unknown };
  observation: Observation;
  reward: number;
  terminated: boolean;
  truncated: boolean;
}

export interface SessionState {
  session_id: string;
  task_id: string;
  read_only: boolean;
  steps: StepEntry[];
}

export interface ApiError {
  error: {
    code: string;
    message: string;
    fields?: Record<string, string[]>;
  };
  detail?: string;
}

export const ACTION_TOOLS = [
  "run_sql",
  "describe_table",
  "describe_tables",
  "describe_schema",
  "submit_final_answer",
  "run_rag_query",
] as const;

export type EmbeddingState = "unconfigured" | "missing" | "stale" | "ready";

export interface EmbeddingJob {
  job_id: string;
  state: "running" | "done" | "validation_failed" | "failed";
  completed: number;
  remaining: number;
  total: number;
  detail?: string | null;
}

export interface EmbeddingStatus {
  state: EmbeddingState;
  states: EmbeddingState[];
  config: {
    base_url: string;
    model: string;
    dim: number;
    max_model_tokens: number;
    chunk_chars: number;
    chunk_overlap: number;
  } | null;
  embedded: number;
  total: number;
  job: EmbeddingJob | null;
  message?: string | null;
}

export interface AvailableAction {
  tool: string;
  summary: string;
  arguments: Record<string, string>;
  example: { tool: string; arguments: Record<string, unknown> };
}

export interface ObservationSpaceEntry {
  type: string;
  description: string;
  example: Observation;
}

export function readAvailableActions(observation: Observation | undefined): AvailableAction[] {
  const meta = observation?.meta as { available_actions?: AvailableAction[] } | undefined;
  return meta?.available_actions ?? [];
}

export function readObservationSpace(observation: Observation | undefined): ObservationSpaceEntry[] {
  const meta = observation?.meta as { observation_space?: ObservationSpaceEntry[] } | undefined;
  return meta?.observation_space ?? [];
}
