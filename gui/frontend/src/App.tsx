import { useEffect, useState } from "react";
import { TaskList } from "./components/TaskList";
import { TaskEditor } from "./components/TaskEditor";
import { TaskDetail } from "./components/TaskDetail";
import { Playground } from "./components/Playground";
import { EmbeddingBanner } from "./components/EmbeddingBanner";
import { getHealth } from "./api";

type View = "list" | "editor" | "detail";

export default function App() {
  const [view, setView] = useState<View>("list");
  const [tab, setTab] = useState<"tasks" | "playground">("tasks");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [health, setHealth] = useState<{ status: string; version: string; database: boolean } | null>(null);

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null));
  }, [refreshKey]);

  function handleSaved(taskId: string) {
    setRefreshKey((k) => k + 1);
    setEditingId(null);
    setView("detail");
    setDetailId(taskId);
  }

  return (
    <div className="app">
      <nav className="navbar navbar-expand navbar-dark bg-dark">
        <div className="container">
          <span className="navbar-brand mb-0 h1">DBArena Admin</span>
          <ul className="navbar-nav me-auto">
            <li className="nav-item">
              <a
                href="#"
                className={`nav-link${tab === "tasks" ? " active" : ""}`}
                onClick={(e) => {
                  e.preventDefault();
                  setTab("tasks");
                }}
              >
                Tasks
              </a>
            </li>
            <li className="nav-item">
              <a
                href="#"
                className={`nav-link${tab === "playground" ? " active" : ""}`}
                onClick={(e) => {
                  e.preventDefault();
                  setTab("playground");
                }}
              >
                Playground
              </a>
            </li>
          </ul>
          {health && (
            <span className="navbar-text small">
              backend {health.status} · v{health.version} · admin db{" "}
              {health.database ? "connected" : "unreachable"}
            </span>
          )}
        </div>
      </nav>
      <main className="container py-4">
        <EmbeddingBanner />
        {tab === "playground" && <Playground />}
        {tab === "tasks" && view === "list" && (
          <TaskList
            refreshKey={refreshKey}
            onOpen={(id) => {
              setDetailId(id);
              setView("detail");
            }}
            onEdit={(id) => {
              setEditingId(id);
              setView("editor");
            }}
            onNew={() => {
              setEditingId(null);
              setView("editor");
            }}
          />
        )}
        {tab === "tasks" && view === "editor" && (
          <TaskEditor
            editingId={editingId}
            onSaved={handleSaved}
            onCancel={() => setView("list")}
          />
        )}
        {tab === "tasks" && view === "detail" && detailId && (
          <>
            <button type="button" className="btn btn-outline-secondary btn-sm mb-3" onClick={() => setView("list")}>
              ← Back to list
            </button>
            <TaskDetail taskId={detailId} />
          </>
        )}
      </main>
    </div>
  );
}
