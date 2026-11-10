import { useCallback, useEffect, useState } from "react";
import type { Task } from "../types";
import { CATEGORIES, TASK_TYPES } from "../types";
import { deleteTask, listTasks } from "../api";

const CATEGORY_BADGE: Record<string, string> = {
  OLTP: "text-bg-dark",
  OLAP: "text-bg-primary",
  ANALYTICS: "text-bg-info",
  RAG: "text-bg-warning",
};

export function TaskList({
  onOpen,
  onEdit,
  onNew,
  refreshKey,
}: {
  onOpen: (taskId: string) => void;
  onEdit: (taskId: string) => void;
  onNew: () => void;
  refreshKey: number;
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [categoryFilter, setCategoryFilter] = useState<string>("");
  const [typeFilter, setTypeFilter] = useState<string>("");

  const refresh = useCallback(() => {
    listTasks()
      .then(setTasks)
      .catch((e) => setError(e.message));
  }, []);

  useEffect(refresh, [refresh, refreshKey]);

  async function handleDelete(taskId: string) {
    setError(null);
    try {
      await deleteTask(taskId);
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="task-list">
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h1 className="h3 mb-0">Tasks</h1>
        <div className="d-flex gap-2">
          <select className="form-select form-select-sm w-auto" value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)}>
            <option value="">All categories</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <select className="form-select form-select-sm w-auto" value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
            <option value="">All types</option>
            {TASK_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <button type="button" className="btn btn-primary btn-sm" onClick={onNew}>
            New task
          </button>
          <button type="button" className="btn btn-outline-secondary btn-sm" onClick={refresh}>
            Refresh
          </button>
        </div>
      </div>
      {error && <div className="alert alert-danger py-2">{error}</div>}
      <div className="card shadow-sm">
        <div className="table-responsive">
          <table className="table table-striped table-hover align-middle mb-0">
            <thead className="table-light">
              <tr>
                <th scope="col">Task ID</th>
                <th scope="col">Category</th>
                <th scope="col">Type</th>
                <th scope="col">Goal</th>
                <th scope="col">Max steps</th>
                <th scope="col">Timeout (s)</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {tasks
                .filter((t) => !categoryFilter || t.category === categoryFilter)
                .filter((t) => !typeFilter || t.task_type === typeFilter)
                .map((t) => (
                  <tr key={t.task_id}>
                    <td>
                      <a
                        href="#"
                        className="text-decoration-none fw-semibold"
                        onClick={(e) => {
                          e.preventDefault();
                          onOpen(t.task_id);
                        }}
                      >
                        {t.task_id}
                      </a>
                    </td>
                    <td>
                      <span className={`badge ${CATEGORY_BADGE[t.category] ?? "text-bg-secondary"}`}>{t.category}</span>
                    </td>
                    <td>
                      <span className="badge text-bg-light border">{t.task_type}</span>
                    </td>
                    <td className="goal-cell">{t.goal}</td>
                    <td>{t.max_steps}</td>
                    <td>{t.query_timeout}</td>
                    <td>
                      <button type="button" className="btn btn-sm btn-outline-primary me-1" onClick={() => onEdit(t.task_id)}>
                        Edit
                      </button>{" "}
                      <button type="button" className="btn btn-sm btn-danger" onClick={() => handleDelete(t.task_id)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              {tasks.length > 0 &&
                tasks.filter((t) => !categoryFilter || t.category === categoryFilter).filter((t) => !typeFilter || t.task_type === typeFilter).length === 0 && (
                  <tr>
                    <td colSpan={7} className="text-muted">
                      No tasks match the selected filters.
                    </td>
                  </tr>
                )}
              {tasks.length === 0 && !error && (
                <tr>
                  <td colSpan={7} className="text-muted">
                    No tasks yet. Create one to get started.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
