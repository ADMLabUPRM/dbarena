import { useMemo, useState } from "react";
import { marked } from "marked";
import type { Observation } from "../types";
import { toMarkdown } from "../markdown";

export function ObservationView({ observation }: { observation: Observation }) {
  const [mode, setMode] = useState<"markdown" | "raw">("markdown");
  const markdown = useMemo(() => toMarkdown(observation), [observation]);

  const badgeClass =
    observation.type === "error"
      ? "text-bg-danger"
      : observation.type === "episode_result"
        ? "text-bg-success"
        : "text-bg-secondary";

  return (
    <div className="observation-view mt-2">
      <div className="d-flex justify-content-between align-items-center mb-2">
        <span className={`badge ${badgeClass}`}>{observation.type}</span>
        <div className="btn-group btn-group-sm" role="group" aria-label="Observation rendering">
          <button
            type="button"
            className={`btn btn-outline-secondary${mode === "markdown" ? " active" : ""}`}
            onClick={() => setMode("markdown")}
          >
            Markdown
          </button>
          <button
            type="button"
            className={`btn btn-outline-secondary${mode === "raw" ? " active" : ""}`}
            onClick={() => setMode("raw")}
          >
            Raw JSON
          </button>
        </div>
      </div>
      {mode === "markdown" ? (
        <div className="markdown border rounded p-2 bg-white" dangerouslySetInnerHTML={{ __html: marked.parse(markdown) as string }} />
      ) : (
        <pre className="raw-json mb-0">{JSON.stringify(observation, null, 2)}</pre>
      )}
    </div>
  );
}
