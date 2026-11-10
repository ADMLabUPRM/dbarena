import { Fragment, useEffect, useState } from "react";
import type { Task } from "../types";
import { getTask } from "../api";

export function TaskDetail({ taskId }: { taskId: string }) {
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setTask(null);
    setError(null);
    getTask(taskId)
      .then(setTask)
      .catch((e) => setError(e.message));
  }, [taskId]);

  if (error) return <div className="alert alert-danger py-2">{error}</div>;
  if (!task) return <p className="text-muted">Loading…</p>;
  const matching = task.terminal_condition.matching ?? {
    order_sensitive: false,
    numeric_tolerance: null,
    null_handling: "exact",
  };

  return (
    <div className="card shadow-sm">
      <div className="card-body">
        <h1 className="h4 card-title mb-3">Task {task.task_id}</h1>
        <dl className="row mb-0">
          <dt className="col-sm-3">Goal</dt>
          <dd className="col-sm-9">{task.goal}</dd>
          <dt className="col-sm-3">Submission</dt>
          <dd className="col-sm-9">{task.submission}</dd>
          <dt className="col-sm-3">Category</dt>
          <dd className="col-sm-9">{task.category}</dd>
          <dt className="col-sm-3">Type</dt>
          <dd className="col-sm-9">{task.task_type}</dd>
          <dt className="col-sm-3">Max steps</dt>
          <dd className="col-sm-9">{task.max_steps}</dd>
          <dt className="col-sm-3">Query timeout</dt>
          <dd className="col-sm-9">{task.query_timeout} seconds</dd>
          <dt className="col-sm-3">Row limit</dt>
          <dd className="col-sm-9">{task.row_limit === null ? "default (50)" : task.row_limit}</dd>
          <dt className="col-sm-3">Target database</dt>
          <dd className="col-sm-9">
            <code className="text-break">{task.sqlalchemy_url}</code>
          </dd>
          <dt className="col-sm-3">Expected tuples</dt>
          <dd className="col-sm-9">
            <pre className="mb-0 font-monospace small">{JSON.stringify(task.terminal_condition.expected_tuples)}</pre>
          </dd>
          <dt className="col-sm-3">Matching rules</dt>
          <dd className="col-sm-9">
            order_sensitive: {String(matching.order_sensitive)}; numeric_tolerance:{" "}
            {matching.numeric_tolerance === null ? "exact" : matching.numeric_tolerance}; null_handling:{" "}
            {matching.null_handling}
          </dd>
          <dt className="col-sm-3">Verifier</dt>
          <dd className="col-sm-9">
            {(task.terminal_condition.state_checks?.length ?? 0) === 0 &&
            (task.terminal_condition.rules?.length ?? 0) === 0
              ? "Tuple matching only — no state checks or verification rules."
              : `Confirmation tuple, ${task.terminal_condition.state_checks?.length ?? 0} state check(s), ${
                  task.terminal_condition.rules?.length ?? 0
                } verification rule(s)`}
          </dd>
          {task.terminal_condition.state_checks?.map((check, index) => (
            <Fragment key={`state-check-${index}`}>
              <dt className="col-sm-3">State check {index + 1}</dt>
              <dd className="col-sm-9">
                <pre className="mb-1 font-monospace small bg-light p-2 text-break">{check.sql}</pre>
                <div className="small">
                  expected: <code>{JSON.stringify(check.expected_tuples)}</code>
                </div>
                {check.matching && (
                  <div className="small text-muted">
                    matching: order_sensitive {String(check.matching.order_sensitive)}; numeric_tolerance{" "}
                    {check.matching.numeric_tolerance === null || check.matching.numeric_tolerance === undefined
                      ? "exact"
                      : check.matching.numeric_tolerance}
                    ; null_handling {check.matching.null_handling ?? "exact"}
                  </div>
                )}
              </dd>
            </Fragment>
          ))}
          {task.terminal_condition.rules?.map((rule, index) => (
            <Fragment key={`rule-${index}`}>
              <dt className="col-sm-3">
                Rule {index + 1}
                {rule.description ? `: ${rule.description}` : ""}
              </dt>
              <dd className="col-sm-9">
                <pre className="mb-0 font-monospace small bg-light p-2 text-break">{rule.sql}</pre>
              </dd>
            </Fragment>
          ))}
        </dl>
      </div>
    </div>
  );
}
