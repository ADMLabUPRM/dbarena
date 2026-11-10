import type { ApiError, EmbeddingStatus, SessionState, StepEntry, Task, TaskPayload } from "./types";

async function handle<T>(response: Response): Promise<T> {
  if (response.status === 204) {
    return undefined as T;
  }
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    const text = await response.text();
    throw new Error(
      `Backend returned a non-JSON response (HTTP ${response.status} ${response.statusText}). ` +
        `Is the API reachable from this origin? Response started with: ${text.slice(0, 80)}`,
    );
  }
  const body = await response.json();
  if (!response.ok) {
    const apiError = body as ApiError;
    if (apiError?.error) {
      throw Object.assign(new Error(apiError.error.message), { apiError });
    }
    throw Object.assign(new Error(body?.detail || response.statusText), { detail: body?.detail });
  }
  return body as T;
}

export async function getHealth(): Promise<{ status: string; version: string; database: boolean }> {
  return handle(await fetch("/api/health"));
}

export async function listTasks(): Promise<Task[]> {
  return handle(await fetch("/api/tasks"));
}

export async function getTask(taskId: string): Promise<Task> {
  return handle(await fetch(`/api/tasks/${encodeURIComponent(taskId)}`));
}

export async function createTask(payload: TaskPayload): Promise<Task> {
  return handle(
    await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function updateTask(taskId: string, payload: TaskPayload): Promise<Task> {
  return handle(
    await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function deleteTask(taskId: string): Promise<void> {
  return handle(await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" }));
}

export async function validateTask(
  taskId: string,
  payload: TaskPayload,
): Promise<{ valid: boolean }> {
  return handle(
    await fetch(`/api/tasks/${encodeURIComponent(taskId)}/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function createSession(taskId: string): Promise<SessionState> {
  return handle(
    await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: taskId }),
    }),
  );
}

export async function stepSession(
  sessionId: string,
  tool: string,
  argumentsJson: string,
): Promise<StepEntry> {
  let parsed: unknown = {};
  if (argumentsJson.trim()) {
    parsed = JSON.parse(argumentsJson);
  }
  return handle(
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/step`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool, arguments: parsed }),
    }),
  );
}

export async function resetSession(sessionId: string): Promise<SessionState> {
  return handle(await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/reset`, { method: "POST" }));
}

export async function getSession(sessionId: string): Promise<SessionState> {
  return handle(await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`));
}

export async function getEmbeddingStatus(): Promise<EmbeddingStatus> {
  return handle(await fetch("/api/embeddings/status"));
}

export async function saveEmbeddingConfig(payload: {
  base_url: string;
  model: string;
  dim: number;
  api_key?: string;
  max_model_tokens?: number;
  chunk_chars?: number;
  chunk_overlap?: number;
}): Promise<{
  config: {
    base_url: string;
    model: string;
    dim: number;
    max_model_tokens: number;
    chunk_chars: number;
    chunk_overlap: number;
  };
}> {
  return handle(
    await fetch("/api/embeddings/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function startReembed(): Promise<{ job_id: string; state: string }> {
  return handle(await fetch("/api/embeddings/reembed", { method: "POST" }));
}

export async function getJudgeConfig(): Promise<{
  config: { base_url: string; model: string; api_key_set: boolean } | null;
}> {
  return handle(await fetch("/api/judge/config"));
}

export async function saveJudgeConfig(payload: {
  base_url: string;
  model: string;
  api_key?: string;
}): Promise<{ config: { base_url: string; model: string } }> {
  return handle(
    await fetch("/api/judge/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export type JudgeTestResult =
  | { ok: true; base_url: string; model: string; latency_ms: number; reply: string }
  | { ok: false; error: string };

export async function testJudgeConfig(payload: {
  base_url?: string;
  model?: string;
  api_key?: string;
}): Promise<JudgeTestResult> {
  return handle(
    await fetch("/api/judge/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}
