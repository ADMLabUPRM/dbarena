import { useCallback, useEffect, useState } from "react";
import { getEmbeddingStatus, getJudgeConfig, saveEmbeddingConfig, saveJudgeConfig, startReembed, testJudgeConfig } from "../api";
import type { EmbeddingStatus } from "../types";

const ALERT_LABELS: Record<string, string> = {
  unconfigured: "The embedding model is not configured.",
  missing: "Embeddings are missing for the document corpus.",
  stale: "The embedding configuration changed — re-embedding is required.",
  validation_failed: "The configured model failed the retrieval validation.",
  failed: "The last re-embed job failed.",
};

function problemState(status: EmbeddingStatus): string | null {
  if (status.job && (status.job.state === "validation_failed" || status.job.state === "failed")) {
    return status.job.state;
  }
  if (status.state !== "ready") {
    return status.state;
  }
  return null;
}

function describe(status: EmbeddingStatus): string {
  const problem = problemState(status);
  if (problem === "unconfigured") {
    return ALERT_LABELS.unconfigured;
  }
  if (problem === "missing") {
    return `${ALERT_LABELS.missing} ${status.embedded} of ${status.total} documents embedded.`;
  }
  if (problem === "stale") {
    return `${ALERT_LABELS.stale} ${status.embedded} of ${status.total} documents match the current model.`;
  }
  if (problem === "validation_failed" || problem === "failed") {
    return `${ALERT_LABELS[problem]} ${status.job?.detail ?? ""}`.trim();
  }
  return "ready";
}

export function EmbeddingBanner() {
  const [status, setStatus] = useState<EmbeddingStatus | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getEmbeddingStatus()
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const job = status?.job;
    if (job && job.state === "running") {
      const timer = window.setTimeout(refresh, 1500);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [status, refresh]);

  if (status === null) {
    return null;
  }

  const problem = problemState(status);
  const jobRunning = status.job?.state === "running";
  const modelLabel = status.config
    ? `${status.config.model} (${status.config.dim}d · ${status.config.chunk_chars}c/${status.config.chunk_overlap}o)`
    : "not configured";

  return (
    <>
      {problem !== null && !jobRunning && (
        <div className="alert alert-warning d-flex justify-content-between align-items-center mb-2" role="alert">
          <div>
            <strong>Embeddings:</strong> {describe(status)}
          </div>
          <button type="button" className="btn btn-outline-dark btn-sm" onClick={() => setModalOpen(true)}>
            Configure embedding model
          </button>
        </div>
      )}
      {problem === null && !jobRunning && (
        <div className="d-flex justify-content-between align-items-center text-muted small mb-2">
          <span>
            Embeddings: <span className="text-success">ready</span> · {modelLabel} · {status.embedded}/
            {status.total} documents
          </span>
          <button type="button" className="btn btn-outline-secondary btn-sm" onClick={() => setModalOpen(true)}>
            Configure embedding model
          </button>
        </div>
      )}
      {jobRunning && (
        <div className="alert alert-info mt-2" role="status">
          <strong>Re-embedding corpus…</strong> {status.job!.completed} completed ·{" "}
          {status.job!.remaining} remaining · {status.job!.total} total
          <div className="progress mt-2" style={{ height: "0.75rem" }}>
            <div
              className="progress-bar progress-bar-striped progress-bar-animated"
              style={{ width: `${percent(status)}%` }}
              role="progressbar"
              aria-valuenow={percent(status)}
              aria-valuemin={0}
              aria-valuemax={100}
            />
          </div>
          <button type="button" className="btn btn-link btn-sm p-0" onClick={refresh}>
            refresh now
          </button>
        </div>
      )}
      {error && (
        <div className="alert alert-danger mt-2" role="alert">
          {error}
        </div>
      )}
      {modalOpen && (
        <EmbeddingModal
          status={status}
          onClose={() => setModalOpen(false)}
          onError={(message) => {
            setError(message);
            setModalOpen(false);
          }}
        />
      )}
    </>
  );
}

function percent(status: EmbeddingStatus): number {
  const job = status.job!;
  return job.total > 0 ? Math.round(((job.total - job.remaining) / job.total) * 100) : 0;
}

function EmbeddingModal({
  status,
  onClose,
  onError,
}: {
  status: EmbeddingStatus | null;
  onClose: () => void;
  onError: (message: string) => void;
}) {
  const [baseUrl, setBaseUrl] = useState(status?.config?.base_url ?? "");
  const [model, setModel] = useState(status?.config?.model ?? "");
  const [dim, setDim] = useState(String(status?.config?.dim ?? 1024));
  const [apiKey, setApiKey] = useState("");
  const [maxTokens, setMaxTokens] = useState(String(status?.config?.max_model_tokens ?? 512));
  const [chunkChars, setChunkChars] = useState(String(status?.config?.chunk_chars ?? 800));
  const [chunkOverlap, setChunkOverlap] = useState(String(status?.config?.chunk_overlap ?? 0));
  const [busy, setBusy] = useState(false);
  const [judgeBaseUrl, setJudgeBaseUrl] = useState("");
  const [judgeModel, setJudgeModel] = useState("");
  const [judgeApiKey, setJudgeApiKey] = useState("");
  const [judgeLoaded, setJudgeLoaded] = useState(false);
  const [judgeSaved, setJudgeSaved] = useState(false);
  const [judgeTesting, setJudgeTesting] = useState(false);
  const [judgeTest, setJudgeTest] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    getJudgeConfig()
      .then(({ config }) => {
        if (config) {
          setJudgeBaseUrl(config.base_url);
          setJudgeModel(config.model);
        }
        setJudgeLoaded(true);
      })
      .catch(() => setJudgeLoaded(true));
  }, []);

  const numericDim = Number(dim);
  const numericMaxTokens = Number(maxTokens);
  const numericChunkChars = Number(chunkChars);
  const numericOverlap = Number(chunkOverlap);
  const inputsValid =
    numericDim > 0 &&
    numericMaxTokens > 0 &&
    numericChunkChars > 0 &&
    numericOverlap >= 0 &&
    numericOverlap < numericChunkChars;
  const chunkWarning =
    numericChunkChars > numericMaxTokens * 3
      ? `chunk size of ${numericChunkChars} chars likely exceeds ${numericMaxTokens} tokens for typical prose — the job will adaptively halve on token-limit errors`
      : null;

  async function handleSaveAndReembed() {
    setBusy(true);
    try {
      await saveEmbeddingConfig({
        base_url: baseUrl.trim(),
        model: model.trim(),
        dim: numericDim,
        api_key: apiKey,
        max_model_tokens: numericMaxTokens,
        chunk_chars: numericChunkChars,
        chunk_overlap: numericOverlap,
      });
      await startReembed();
      onClose();
    } catch (exc) {
      onError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveJudge() {
    setBusy(true);
    try {
      await saveJudgeConfig({
        base_url: judgeBaseUrl.trim(),
        model: judgeModel.trim(),
        ...(judgeApiKey.trim() ? { api_key: judgeApiKey.trim() } : {}),
      });
      setJudgeSaved(true);
      setBusy(false);
      onClose();
    } catch (exc) {
      setBusy(false);
      onError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleTestJudge() {
    setJudgeTesting(true);
    setJudgeTest(null);
    try {
      const result = await testJudgeConfig({
        ...(judgeBaseUrl.trim() ? { base_url: judgeBaseUrl.trim() } : {}),
        ...(judgeModel.trim() ? { model: judgeModel.trim() } : {}),
        ...(judgeApiKey.trim() ? { api_key: judgeApiKey.trim() } : {}),
      });
      setJudgeTest(
        result.ok
          ? {
              ok: true,
              text: `Judge LLM reachable — ${result.model} replied in ${(result.latency_ms / 1000).toFixed(1)} s`,
            }
          : { ok: false, text: result.error },
      );
    } catch (exc) {
      setJudgeTest({ ok: false, text: exc instanceof Error ? exc.message : String(exc) });
    } finally {
      setJudgeTesting(false);
    }
  }

  return (
    <div className="modal d-block" style={{ backgroundColor: "rgba(0,0,0,0.4)" }} role="dialog">
      <div className="modal-dialog modal-lg">
        <div className="modal-content">
          <div className="modal-header">
            <h5 className="modal-title">Embedding &amp; RAG settings</h5>
            <button type="button" className="btn-close" onClick={onClose} aria-label="Close" />
          </div>
          <div className="modal-body">
            {status && problemState(status) !== null && (
              <p className="text-muted small">{describe(status)}</p>
            )}
            <h6 className="mt-1">Endpoint</h6>
            <div className="mb-3">
              <label className="form-label" htmlFor="embed-base-url">
                Endpoint base URL (OpenAI-compatible)
              </label>
              <input
                id="embed-base-url"
                className="form-control"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="http://embed-server:30000/v1"
              />
            </div>
            <div className="row">
              <div className="col mb-3">
                <label className="form-label" htmlFor="embed-model">
                  Model name
                </label>
                <input
                  id="embed-model"
                  className="form-control"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="BAAI/bge-large-en-v1.5"
                />
              </div>
              <div className="col-3 mb-3">
                <label className="form-label" htmlFor="embed-dim">
                  Dimension
                </label>
                <input
                  id="embed-dim"
                  className="form-control"
                  value={dim}
                  onChange={(e) => setDim(e.target.value)}
                  inputMode="numeric"
                />
              </div>
            </div>
            <div className="mb-1">
              <label className="form-label" htmlFor="embed-api-key">
                API key (optional)
              </label>
              <input
                id="embed-api-key"
                className="form-control"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
            </div>
            <h6 className="mt-4">Chunking</h6>
            <div className="row">
              <div className="col-4 mb-3">
                <label className="form-label" htmlFor="embed-max-tokens">
                  Max model tokens
                </label>
                <input
                  id="embed-max-tokens"
                  className="form-control"
                  value={maxTokens}
                  onChange={(e) => setMaxTokens(e.target.value)}
                  inputMode="numeric"
                />
              </div>
              <div className="col-4 mb-3">
                <label className="form-label" htmlFor="embed-chunk-chars">
                  Chunk size (chars)
                </label>
                <input
                  id="embed-chunk-chars"
                  className="form-control"
                  value={chunkChars}
                  onChange={(e) => setChunkChars(e.target.value)}
                  inputMode="numeric"
                />
              </div>
              <div className="col-4 mb-3">
                <label className="form-label" htmlFor="embed-chunk-overlap">
                  Chunk overlap (chars)
                </label>
                <input
                  id="embed-chunk-overlap"
                  className="form-control"
                  value={chunkOverlap}
                  onChange={(e) => setChunkOverlap(e.target.value)}
                  inputMode="numeric"
                />
              </div>
            </div>
            <p className="text-muted small mb-0">
              Whole-document passage chunking: every chunk becomes one embedding row. ~3 chars per
              token of typical prose, so keep the chunk size under max tokens × 3. Changing any
              chunking parameter marks the corpus stale and re-embedding rebuilds every vector.
              {chunkWarning && <span className="text-warning d-block">{chunkWarning}</span>}
            </p>
            <h6 className="mt-4">Judge LLM</h6>
            {judgeSaved && (
              <div className="alert alert-success py-2 small" role="status">
                Judge LLM saved — verdicts will use the configured chat server.
              </div>
            )}
            {judgeTest && (
              <div
                className={`alert py-2 small ${judgeTest.ok ? "alert-success" : "alert-danger"}`}
                role="status"
              >
                {judgeTest.ok ? "✓ " : "✗ "}
                {judgeTest.text}
              </div>
            )}
            <p className="text-muted small mb-2">
              The chat model the judge service calls for pass/fail verdicts on natural-language RAG
              tasks (OpenAI-compatible). Leave a field empty to keep its current value.
            </p>
            <div className="mb-3">
              <label className="form-label" htmlFor="judge-base-url">
                Judge endpoint base URL (OpenAI-compatible)
              </label>
              <input
                id="judge-base-url"
                className="form-control"
                value={judgeBaseUrl}
                onChange={(e) => setJudgeBaseUrl(e.target.value)}
                placeholder={judgeLoaded ? "http://your-llm-server" : ""}
              />
            </div>
            <div className="row">
              <div className="col mb-3">
                <label className="form-label" htmlFor="judge-model">
                  Judge model name
                </label>
                <input
                  id="judge-model"
                  className="form-control"
                  value={judgeModel}
                  onChange={(e) => setJudgeModel(e.target.value)}
                  placeholder={judgeLoaded ? "local-sglang/glm-5.3-flash" : ""}
                />
              </div>
              <div className="col mb-3">
                <label className="form-label" htmlFor="judge-api-key">
                  Judge API key (optional)
                </label>
                <input
                  id="judge-api-key"
                  className="form-control"
                  type="password"
                  value={judgeApiKey}
                  onChange={(e) => setJudgeApiKey(e.target.value)}
                  placeholder="leave empty to keep"
                />
              </div>
            </div>
          </div>
          <div className="modal-footer">
            <button type="button" className="btn btn-outline-secondary btn-sm" onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-outline-info btn-sm"
              disabled={busy || judgeTesting || !judgeBaseUrl.trim() || !judgeModel.trim()}
              onClick={handleTestJudge}
            >
              {judgeTesting ? "Testing…" : "Test LLM Judge Connection"}
            </button>
            <button
              type="button"
              className="btn btn-outline-primary btn-sm"
              disabled={busy || !judgeBaseUrl.trim() || !judgeModel.trim()}
              onClick={handleSaveJudge}
            >
              Save judge LLM
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy || !inputsValid || !baseUrl.trim() || !model.trim()}
              onClick={handleSaveAndReembed}
            >
              {busy ? "Working…" : "Save and re-embed"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
