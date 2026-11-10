"""OpenAI-compatible embedding client and the embedding lifecycle model.

Shared by the gym's `embed_text` action, the backend re-embed job, and status
derivation. The client speaks the de-facto standard embeddings API:
``POST {base}/v1/embeddings`` with ``{"model": ..., "input": [...]}`` returning
``{"data": [{"embedding": [...], "index": n}]}`` (OpenAI, SGLang, Ollama, LM
Studio, vLLM, ...). Lifecycle states:

- ``unconfigured``: no configuration persisted
- ``missing``: fewer embedded documents than documents
- ``stale``: all documents embedded, but under a different fingerprint
- ``ready``: all documents embedded under the current fingerprint
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

EMBEDDING_STATES = ("unconfigured", "missing", "stale", "ready")

_ERROR_CODES = {
    "not_configured": "the embedding model is not configured",
    "request_failed": "the embedding request failed",
    "dimension_mismatch": "the embedding dimension does not match the configuration",
}


class EmbeddingError(Exception):
    """Structured embedding failure: code is not_configured/request_failed/dimension_mismatch."""

    def __init__(self, code: str, message: str):
        prefix = _ERROR_CODES.get(code, "embedding error")
        super().__init__(message or prefix)
        self.code = code


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    dim: int
    api_key: str = ""
    max_model_tokens: int = 512
    chunk_chars: int = 800
    chunk_overlap: int = 0

    # Strategy tag included in the fingerprint: any change to chunking semantics
    # must invalidate stored embeddings so the corpus re-embeds.
    STRATEGY = "passage-v1"

    @property
    def fingerprint(self) -> str:
        material = (
            f"{self.base_url}|{self.model}|{self.dim}|{self.max_model_tokens}"
            f"|{self.chunk_chars}|{self.chunk_overlap}|{self.STRATEGY}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    @property
    def endpoint_url(self) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/embeddings" if base.endswith("/v1") else f"{base}/v1/embeddings"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> EmbeddingConfig | None:
        env = dict(os.environ if env is None else env)
        base_url = (env.get("EMBEDDING_BASE_URL") or "").strip()
        model = (env.get("EMBEDDING_MODEL") or "").strip()
        if not base_url or not model:
            return None
        try:
            dim = int((env.get("EMBEDDING_DIM") or "").strip())
        except ValueError:
            return None
        if dim <= 0:
            return None
        return cls(
            base_url=base_url,
            model=model,
            dim=dim,
            api_key=env.get("EMBEDDING_API_KEY", ""),
            max_model_tokens=_int_env(env, "EMBEDDING_MAX_MODEL_TOKENS", 512),
            chunk_chars=_int_env(env, "EMBEDDING_CHUNK_CHARS", 800),
            chunk_overlap=_int_env(env, "EMBEDDING_CHUNK_OVERLAP", 0),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "dim": self.dim,
            "max_model_tokens": self.max_model_tokens,
            "chunk_chars": self.chunk_chars,
            "chunk_overlap": self.chunk_overlap,
        }


def _int_env(env: dict[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Sliding-window chunking over the whole text (no truncation).

    Step is size - overlap (minimum 1); the tail is always included, so every
    character of the document appears in at least one chunk.
    """
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("chunk overlap must be >= 0 and smaller than the chunk size")
    text = text or ""
    if len(text) <= size:
        return [text]
    step = size - overlap
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        if start + size >= len(text):
            break
        start += step
    return chunks


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    model: str
    dim: int

    @property
    def vector_literal(self) -> str:
        """pgvector text literal ('[0.1,0.2,...]'), pasteable straight into SQL."""
        return "[" + ",".join(repr(float(value)) for value in self.vector) + "]"


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


class EmbeddingClient:
    def __init__(
        self,
        config: EmbeddingConfig,
        timeout: float = 30.0,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
    ):
        self.config = config
        self.timeout = timeout
        self._opener = opener or _default_opener

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        if not texts:
            return []
        payload = json.dumps({"model": self.config.model, "input": list(texts)}).encode("utf-8")
        request = urllib.request.Request(
            self.config.endpoint_url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self.config.api_key:
            request.add_header("Authorization", f"Bearer {self.config.api_key}")
        try:
            with self._opener(request, self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise EmbeddingError(
                "request_failed", f"embedding endpoint returned HTTP {exc.code}: {detail}"
            ) from None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise EmbeddingError("request_failed", f"embedding endpoint unreachable: {exc}") from None

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError(
                "request_failed",
                f"embedding endpoint returned {len(data) if isinstance(data, list) else 'no'} results "
                f"for {len(texts)} inputs",
            )
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        results = []
        for item in ordered:
            vector = item.get("embedding")
            if not isinstance(vector, list) or len(vector) != self.config.dim:
                actual = len(vector) if isinstance(vector, list) else "non-list"
                raise EmbeddingError(
                    "dimension_mismatch",
                    f"embedding endpoint returned dimension {actual}, configured dimension is "
                    f"{self.config.dim}",
                )
            results.append(
                EmbeddingResult(
                    vector=[float(value) for value in vector],
                    model=self.config.model,
                    dim=self.config.dim,
                )
            )
        return results


def derive_status(
    config: EmbeddingConfig | None,
    embedded_count: int,
    total_count: int,
    stale_count: int,
) -> str:
    if config is None:
        return "unconfigured"
    if embedded_count < total_count:
        return "missing"
    if stale_count > 0:
        return "stale"
    return "ready"
