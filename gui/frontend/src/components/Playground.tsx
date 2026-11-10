import { useEffect, useState } from "react";
import type { SessionState, StepEntry, Task } from "../types";
import { ACTION_TOOLS, CATEGORIES, TASK_TYPES, readAvailableActions, readObservationSpace } from "../types";
import { createSession, getSession, listTasks, resetSession, stepSession } from "../api";
import { ObservationView } from "./ObservationView";

const DEFAULT_ARGUMENTS: Record<string, string> = {
  run_sql: '{\n  "sql": "SELECT 1"\n}',
  describe_table: '{\n  "table": "region"\n}',
  describe_tables: '{\n  "tables": ["region", "nation"]\n}',
  describe_schema: "{}",
  submit_final_answer: '{\n  "tuples": []\n}',
};

function lastStep(steps: StepEntry[]): StepEntry | undefined {
  return steps.length ? steps[steps.length - 1] : undefined;
}

export function Playground() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [taskId, setTaskId] = useState<string>("");
  const [categoryFilter, setCategoryFilter] = useState<string>("");
  const [typeFilter, setTypeFilter] = useState<string>("");
  const [session, setSession] = useState<SessionState | null>(null);
  const [tool, setTool] = useState<string>("describe_schema");
  const [argumentsJson, setArgumentsJson] = useState<string>(DEFAULT_ARGUMENTS["describe_schema"]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    listTasks()
      .then(setTasks)
      .catch((e) => setError(e.message));
  }, []);

  const filteredTasks = tasks
    .filter((t) => !categoryFilter || t.category === categoryFilter)
    .filter((t) => !typeFilter || t.task_type === typeFilter);

  function pickTool(next: string) {
    setTool(next);
    setArgumentsJson(DEFAULT_ARGUMENTS[next] ?? "{}");
  }

  async function handleStart() {
    setError(null);
    setBusy(true);
    try {
      setSession(await createSession(taskId));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleReopen() {
    setError(null);
    const id = window.prompt("Session ID to reopen");
    if (!id) return;
    try {
      setSession(await getSession(id));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function handleStep() {
    if (!session) return;
    setError(null);
    setBusy(true);
    try {
      const entry = await stepSession(session.session_id, tool, argumentsJson);
      setSession({ ...session, steps: [...session.steps, entry] });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleReset() {
    if (!session) return;
    setError(null);
    setBusy(true);
    try {
      setSession(await resetSession(session.session_id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const last = session ? lastStep(session.steps) : undefined;
  const ended = !!last && (last.terminated || last.truncated);
  const endReason = last?.terminated
    ? last.reward === 1.0
      ? "Episode ended: submitted answer matched the terminal condition."
      : "Episode ended: submitted answer did not match the terminal condition."
    : last?.truncated
      ? "Episode ended: max steps reached (truncated)."
      : null;

  const initial = session?.steps[0]?.observation;
  const taskBlock = initial?.task ?? null;
  const actions = readAvailableActions(initial);
  const space = readObservationSpace(initial);

  function useExample(tool: string, args: Record<string, unknown>) {
    pickTool(tool);
    setArgumentsJson(JSON.stringify(args, null, 2));
  }

  return (
    <div className="playground">
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h1 className="h3 mb-0">Playground</h1>
        <div className="d-flex gap-2">
          <select
            className="form-select form-select-sm w-auto"
            value={categoryFilter}
            onChange={(e) => {
              setCategoryFilter(e.target.value);
              const match = filteredTasks.find((t) => t.task_id === taskId && (!e.target.value || t.category === e.target.value));
              if (!match) setTaskId("");
            }}
          >
            <option value="">All categories</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <select
            className="form-select form-select-sm w-auto"
            value={typeFilter}
            onChange={(e) => {
              setTypeFilter(e.target.value);
              const match = filteredTasks.find((t) => t.task_id === taskId && (!e.target.value || t.task_type === e.target.value));
              if (!match) setTaskId("");
            }}
          >
            <option value="">All types</option>
            {TASK_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <select className="form-select form-select-sm w-auto" value={taskId} onChange={(e) => setTaskId(e.target.value)}>
            <option value="">Select a task…</option>
            {filteredTasks.map((t) => (
              <option key={t.task_id} value={t.task_id}>
                {t.task_id} ({t.category} · {t.task_type})
              </option>
            ))}
          </select>
          <button type="button" className="btn btn-primary btn-sm" onClick={handleStart} disabled={!taskId || busy}>
            Start session
          </button>
          <button type="button" className="btn btn-outline-secondary btn-sm" onClick={handleReopen}>
            Reopen by ID
          </button>
        </div>
      </div>
      {error && <div className="alert alert-danger py-2">{error}</div>}
      {session && (
        <>
          <div className="d-flex justify-content-between align-items-center bg-light border rounded p-2 mb-2">
            <span className="small">
              Session <code>{session.session_id}</code> — task <strong>{session.task_id}</strong>
            </span>
            <button type="button" className="btn btn-sm btn-outline-secondary" onClick={handleReset} disabled={session.read_only || busy}>
              Reset session
            </button>
          </div>
          {taskBlock && (
            <div className="alert alert-secondary py-2">
              <strong>Goal:</strong> {taskBlock.goal}
              {taskBlock.submission && (
                <>
                  <br />
                  <strong>Submission:</strong> <span className="small">{taskBlock.submission}</span>
                </>
              )}
            </div>
          )}
          {session.read_only && (
            <div className="alert alert-info py-2">
              This session was restored after a backend restart and is read-only. Start a new session to continue.
            </div>
          )}
          {ended && (
            <div className={`alert py-2 ${last?.terminated && last.reward === 1.0 ? "alert-success" : "alert-warning"}`}>
              {endReason}
            </div>
          )}
          {actions.length > 0 && (
            <div className="card mb-3">
              <div className="card-header bg-white">
                <strong className="small">Available actions</strong>
              </div>
              <div className="card-body py-2">
                {actions.map((a) => (
                  <div key={a.tool} className="d-flex align-items-start gap-2 border-bottom py-2">
                    <div className="flex-grow-1">
                      <code className="fw-semibold">{a.tool}</code>
                      <div className="small">{a.summary}</div>
                      {Object.keys(a.arguments).length > 0 && (
                        <div className="small text-muted">
                          Arguments:{" "}
                          {Object.entries(a.arguments)
                            .map(([name, desc]) => `${name} — ${desc}`)
                            .join("; ")}
                        </div>
                      )}
                    </div>
                    <button
                      type="button"
                      className="btn btn-sm btn-outline-secondary"
                      onClick={() => useExample(a.example.tool, a.example.arguments)}
                      disabled={session.read_only || ended || busy}
                    >
                      Use example
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
          {space.length > 0 && (
            <details className="mb-3">
              <summary className="text-muted small">Observation space reference</summary>
              <ul className="list-group list-group-flush mt-2">
                {space.map((e) => (
                  <li key={e.type} className="list-group-item px-0 small">
                    <span className="badge text-bg-secondary me-2">{e.type}</span>
                    {e.description}
                  </li>
                ))}
              </ul>
            </details>
          )}
          <div className="card mb-3">
            <div className="card-body">
              <div className="row g-2 align-items-end">
                <div className="col-auto">
                  <label className="form-label mb-0 small">Action</label>
                  <select
                    className="form-select form-select-sm"
                    value={tool}
                    onChange={(e) => pickTool(e.target.value)}
                    disabled={session.read_only || ended || busy}
                  >
                    {ACTION_TOOLS.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="col">
                  <label className="form-label mb-0 small">Arguments (JSON)</label>
                  <textarea
                    className="form-control form-control-sm font-monospace"
                    rows={3}
                    value={argumentsJson}
                    onChange={(e) => setArgumentsJson(e.target.value)}
                    disabled={session.read_only || ended || busy}
                  />
                </div>
                <div className="col-auto">
                  <button type="button" className="btn btn-primary btn-sm" onClick={handleStep} disabled={session.read_only || ended || busy}>
                    Step
                  </button>
                </div>
              </div>
            </div>
          </div>
          <ol className="list-group list-group-flush step-log">
            {session.steps.map((step) => (
              <li key={step.step_number} className="list-group-item px-0">
                <div className="d-flex align-items-center gap-2 mb-1">
                  <strong className="small">Step {step.step_number}</strong>
                  <code className="small">{step.action.tool}</code>
                  {step.reward !== 0 && <span className="badge text-bg-success">reward {step.reward}</span>}
                  {step.terminated && <span className="badge text-bg-dark">terminated</span>}
                  {step.truncated && <span className="badge text-bg-warning">truncated</span>}
                </div>
                <ObservationView observation={step.observation} />
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}
