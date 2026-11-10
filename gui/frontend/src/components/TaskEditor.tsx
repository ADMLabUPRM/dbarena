import { useEffect, useState } from "react";
import type { MatchingRules, StateCheck, Task, TaskPayload, VerificationRule } from "../types";
import { CATEGORIES, TASK_TYPES } from "../types";
import { createTask, getTask, updateTask, validateTask } from "../api";

const EMPTY_URL = "postgresql+psycopg://dbarena:dbarena@taskdb:5432/taskdb";

const formShape = {
  taskId: "",
  goal: "",
  submission: "",
  category: "OLAP",
  taskType: "IS",
  maxSteps: "10",
  url: EMPTY_URL,
  tuplesJson: "[]",
  stateChecksJson: "[]",
  rulesJson: "[]",
  orderSensitive: false,
  tolerance: "",
  judgeExpected: "",
  judgeKeyPointsJson: "[]",
  judgeRewardMode: "sparse",
  queryTimeout: "30",
  rowLimit: "",
};

class JsonParseError extends Error {
  constructor(
    public field: string,
    message: string,
  ) {
    super(message);
  }
}

function parseJsonArray<T = unknown>(text: string, field: string): T[] {
  if (!text.trim()) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    throw new JsonParseError(field, e instanceof Error ? e.message : String(e));
  }
  if (!Array.isArray(parsed)) {
    throw new JsonParseError(field, "must be a JSON array");
  }
  return parsed as T[];
}

function parseTupleArray(text: string, field: string): unknown[][] {
  const parsed = parseJsonArray(text, field);
  if (!parsed.every((row) => Array.isArray(row))) {
    throw new JsonParseError(field, "must be an array of tuples (arrays of values)");
  }
  return parsed as unknown[][];
}

function asPayload(form: typeof formShape): TaskPayload {
  const judgeExpected = form.judgeExpected.trim();
  if (judgeExpected) {
    const keyPoints = parseJsonArray<string>(form.judgeKeyPointsJson, "terminal_condition.judge.key_points");
    const terminal: TaskPayload["terminal_condition"] = {
      judge: {
        expected: judgeExpected,
        ...(keyPoints.length ? { key_points: keyPoints.filter((point) => point.trim()) } : {}),
        ...(form.judgeRewardMode === "dense" ? { reward_mode: "dense" } : {}),
      },
    };
    return {
      task_id: form.taskId.trim(),
      goal: form.goal,
      submission: form.submission,
      category: form.category,
      task_type: form.taskType,
      max_steps: Number(form.maxSteps),
      sqlalchemy_url: form.url.trim(),
      terminal_condition: terminal,
      query_timeout: Number(form.queryTimeout),
      row_limit: form.rowLimit.trim() ? Number(form.rowLimit) : null,
    };
  }
  const tuples = parseTupleArray(form.tuplesJson, "terminal_condition.expected_tuples");
  const stateChecks = parseJsonArray<StateCheck>(form.stateChecksJson, "terminal_condition.state_checks");
  const rules = parseJsonArray<VerificationRule>(form.rulesJson, "terminal_condition.rules");
  let tolerance: number | null = null;
  if (form.tolerance.trim()) {
    tolerance = Number(form.tolerance);
  }
  let rowLimit: number | null = null;
  if (form.rowLimit.trim()) {
    rowLimit = Number(form.rowLimit);
  }
  const matching: MatchingRules = {
    order_sensitive: form.orderSensitive,
    numeric_tolerance: tolerance,
    null_handling: "exact",
  };
  return {
    task_id: form.taskId.trim(),
    goal: form.goal,
    submission: form.submission,
    category: form.category,
    task_type: form.taskType,
    max_steps: Number(form.maxSteps),
    sqlalchemy_url: form.url.trim(),
    terminal_condition: {
      expected_tuples: tuples,
      matching,
      ...(stateChecks.length ? { state_checks: stateChecks } : {}),
      ...(rules.length ? { rules } : {}),
    },
    query_timeout: Number(form.queryTimeout),
    row_limit: rowLimit,
  };
}

export function TaskEditor({
  editingId,
  onSaved,
  onCancel,
}: {
  editingId: string | null;
  onSaved: (taskId: string) => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState(formShape);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string[]>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setFieldErrors({});
    setMessage(null);
    if (editingId) {
      setLoading(true);
      getTask(editingId)
        .then((t: Task) => {
          setForm({
            taskId: t.task_id,
            goal: t.goal,
            submission: t.submission,
            category: t.category,
            taskType: t.task_type,
            maxSteps: String(t.max_steps),
            url: t.sqlalchemy_url,
            tuplesJson: JSON.stringify(t.terminal_condition.expected_tuples ?? [], null, 2),
            stateChecksJson: JSON.stringify(t.terminal_condition.state_checks ?? [], null, 2),
            rulesJson: JSON.stringify(t.terminal_condition.rules ?? [], null, 2),
            orderSensitive: t.terminal_condition.matching?.order_sensitive ?? false,
            tolerance:
              t.terminal_condition.matching?.numeric_tolerance === null ||
              t.terminal_condition.matching?.numeric_tolerance === undefined
                ? ""
                : String(t.terminal_condition.matching.numeric_tolerance),
            judgeExpected: t.terminal_condition.judge?.expected ?? "",
            judgeKeyPointsJson: JSON.stringify(t.terminal_condition.judge?.key_points ?? [], null, 2),
            judgeRewardMode: t.terminal_condition.judge?.reward_mode ?? "sparse",
            queryTimeout: String(t.query_timeout),
            rowLimit: t.row_limit === null ? "" : String(t.row_limit),
          });
        })
        .catch((e) => setMessage(e.message))
        .finally(() => setLoading(false));
    } else {
      setForm({ ...formShape });
    }
  }, [editingId]);

  function set<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function handleSave() {
    setFieldErrors({});
    setMessage(null);
    let payload: TaskPayload;
    try {
      payload = asPayload(form);
    } catch (e) {
      if (e instanceof JsonParseError) {
        setFieldErrors({ [e.field]: [e.message] });
        return;
      }
      setFieldErrors({ "terminal_condition.expected_tuples": ["must be valid JSON array of tuples"] });
      return;
    }
    try {
      if (editingId) {
        await updateTask(editingId, payload);
      } else {
        await createTask(payload);
      }
      onSaved(payload.task_id);
    } catch (e) {
      const err = e as Error & { apiError?: { fields?: Record<string, string[]> } };
      if (err.apiError?.fields) {
        setFieldErrors(err.apiError.fields);
      } else {
        setMessage(err.message);
      }
    }
  }

  async function handleValidate() {
    setFieldErrors({});
    setMessage(null);
    let payload: TaskPayload;
    try {
      payload = asPayload(form);
    } catch (e) {
      if (e instanceof JsonParseError) {
        setFieldErrors({ [e.field]: [e.message] });
        return;
      }
      setFieldErrors({ "terminal_condition.expected_tuples": ["must be valid JSON array of tuples"] });
      return;
    }
    try {
      const result = await validateTask(form.taskId.trim(), payload);
      setMessage(result.valid ? "Task is valid." : "Task is invalid.");
    } catch (e) {
      const err = e as Error & { apiError?: { fields?: Record<string, string[]> } };
      if (err.apiError?.fields) {
        setFieldErrors(err.apiError.fields);
      } else {
        setMessage(err.message);
      }
    }
  }

  function fieldError(name: string) {
    const messages = fieldErrors[name];
    return messages ? <div className="invalid-feedback d-block">{messages.join("; ")}</div> : null;
  }

  return (
    <div className="card shadow-sm">
      <div className="card-body">
        <h1 className="h4 card-title mb-3">{editingId ? `Edit task ${editingId}` : "New task"}</h1>
        {loading && <p className="text-muted">Loading…</p>}
        <div className="mb-3">
          <label className="form-label">Task ID</label>
          <input
            className="form-control"
            value={form.taskId}
            onChange={(e) => set("taskId", e.target.value)}
            disabled={!!editingId}
          />
          {fieldError("task_id")}
        </div>
        <div className="mb-3">
          <label className="form-label">Goal (natural language)</label>
          <textarea className="form-control" rows={3} value={form.goal} onChange={(e) => set("goal", e.target.value)} />
          {fieldError("goal")}
        </div>
        <div className="mb-3">
          <label className="form-label">Submission instructions (how the agent must submit the answer)</label>
          <textarea
            className="form-control"
            rows={2}
            value={form.submission}
            onChange={(e) => set("submission", e.target.value)}
          />
          {fieldError("submission")}
        </div>
        <div className="row g-3 mb-1">
          <div className="col-md-4">
            <label className="form-label">Category</label>
            <select className="form-select" value={form.category} onChange={(e) => set("category", e.target.value)}>
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
            {fieldError("category")}
          </div>
          <div className="col-md-4">
            <label className="form-label">Type</label>
            <select className="form-select" value={form.taskType} onChange={(e) => set("taskType", e.target.value)}>
              {TASK_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            {fieldError("task_type")}
          </div>
          <div className="col-md-4">
            <label className="form-label">Max steps</label>
            <input type="number" className="form-control" value={form.maxSteps} onChange={(e) => set("maxSteps", e.target.value)} />
            {fieldError("max_steps")}
          </div>
          <div className="col-md-4">
            <label className="form-label">Query timeout (seconds)</label>
            <input type="number" className="form-control" value={form.queryTimeout} onChange={(e) => set("queryTimeout", e.target.value)} />
            {fieldError("query_timeout")}
          </div>
          <div className="col-md-4">
            <label className="form-label">Row limit (optional)</label>
            <input className="form-control" value={form.rowLimit} onChange={(e) => set("rowLimit", e.target.value)} />
            {fieldError("row_limit")}
          </div>
        </div>
        <div className="mb-3">
          <label className="form-label">SQLAlchemy URL</label>
          <input className="form-control font-monospace" value={form.url} onChange={(e) => set("url", e.target.value)} />
          {fieldError("sqlalchemy_url")}
        </div>
        <div className="card bg-light border-0 mb-3">
          <div className="card-body">
            <h2 className="h6 card-title">Terminal condition</h2>
            <div className="mb-3">
              <label className="form-label">
                Expected tuples (JSON array of tuples; an empty array means an empty result is expected)
              </label>
              <textarea
                className="form-control font-monospace"
                rows={5}
                value={form.tuplesJson}
                onChange={(e) => set("tuplesJson", e.target.value)}
              />
              {fieldError("terminal_condition")}
              {fieldError("terminal_condition.expected_tuples")}
            </div>
            <div className="row g-3">
              <div className="col-md-6">
                <div className="form-check">
                  <input
                    type="checkbox"
                    className="form-check-input"
                    id="order-sensitive-check"
                    checked={form.orderSensitive}
                    onChange={(e) => set("orderSensitive", e.target.checked)}
                  />
                  <label className="form-check-label" htmlFor="order-sensitive-check">
                    Order sensitive
                  </label>
                </div>
              </div>
              <div className="col-md-6">
                <label className="form-label">Numeric tolerance (optional)</label>
                <input className="form-control" value={form.tolerance} onChange={(e) => set("tolerance", e.target.value)} />
                {fieldError("terminal_condition.matching")}
              </div>
            </div>
            <div className="mb-3 mt-3">
              <label className="form-label">
                State checks (optional JSON array of {"{sql, expected_tuples, matching?}"} — each query must return its
                expected tuples)
              </label>
              <textarea
                className="form-control font-monospace"
                rows={5}
                value={form.stateChecksJson}
                onChange={(e) => set("stateChecksJson", e.target.value)}
              />
              {fieldError("terminal_condition.state_checks")}
            </div>
            <div className="mb-3">
              <label className="form-label">
                Verification rules (optional JSON array of {"{sql, description?}"} — each query must return exactly one
                row, one column, boolean true)
              </label>
              <textarea
                className="form-control font-monospace"
                rows={5}
                value={form.rulesJson}
                onChange={(e) => set("rulesJson", e.target.value)}
              />
              {fieldError("terminal_condition.rules")}
            </div>
            <div className="mb-1">
              <label className="form-label">
                Judge expected result (optional — for LLM-judged tasks: the authored reference answer the judge
                checks the submitted natural-language answer against)
              </label>
              <textarea
                className="form-control font-monospace"
                rows={4}
                value={form.judgeExpected}
                onChange={(e) => set("judgeExpected", e.target.value)}
                placeholder="Leave empty for tuple-verified tasks. When set, the task is judge-verified and expected tuples are ignored."
              />
              {fieldError("terminal_condition.judge")}
              {fieldError("terminal_condition.judge.expected")}
            </div>
            <div className="mb-3">
              <label className="form-label">
                Judge key points (optional JSON array of strings — enables dense rewards: the judge grades
                the answer by how many key points it conveys; each key point should cite its incident id)
              </label>
              <textarea
                className="form-control font-monospace"
                rows={4}
                value={form.judgeKeyPointsJson}
                onChange={(e) => set("judgeKeyPointsJson", e.target.value)}
              />
              {fieldError("terminal_condition.judge.key_points")}
            </div>
            <div className="mb-1">
              <label className="form-label" htmlFor="judge-reward-mode">
                Judge reward mode
              </label>
              <select
                id="judge-reward-mode"
                className="form-select"
                value={form.judgeRewardMode}
                onChange={(e) => set("judgeRewardMode", e.target.value)}
              >
                <option value="sparse">sparse — binary pass/fail (1.0 or 0.0)</option>
                <option value="dense">dense — graded by key-point coverage (requires key points)</option>
              </select>
              {fieldError("terminal_condition.judge.reward_mode")}
            </div>
          </div>
        </div>
        {message && <div className="alert alert-info py-2">{message}</div>}
        <div className="d-flex gap-2">
          <button type="button" className="btn btn-primary" onClick={handleSave}>
            Save
          </button>
          <button type="button" className="btn btn-outline-primary" onClick={handleValidate}>
            Validate
          </button>
          <button type="button" className="btn btn-outline-secondary" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
