import io
import json

import pytest

from dbarena.embeddings import (
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    chunk_text,
    derive_status,
)


def _config(**overrides) -> EmbeddingConfig:
    values = {
        "base_url": "http://embed:30000/v1",
        "model": "bge-m3",
        "dim": 4,
        "api_key": "",
    }
    values.update(overrides)
    return EmbeddingConfig(**values)


def _fake_opener(responses):
    """responses: list of (status, body-bytes) or exception instances, one per call."""
    calls = []

    def opener(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        outcome = responses[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome

        class _Response:
            def __init__(self, status, body):
                self.status = status
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        import urllib.error

        if outcome[0] != 200:
            raise urllib.error.HTTPError(request.full_url, outcome[0], "error", None, io.BytesIO(outcome[1]))
        return _Response(outcome[0], outcome[1])

    return opener, calls


def test_endpoint_url_variants():
    assert _config().endpoint_url == "http://embed:30000/v1/embeddings"
    assert _config(base_url="http://embed:30000").endpoint_url == "http://embed:30000/v1/embeddings"
    assert _config(base_url="http://embed:30000/").endpoint_url == "http://embed:30000/v1/embeddings"


def test_fingerprint_changes_with_model():
    assert _config().fingerprint != _config(model="other").fingerprint
    assert _config().fingerprint == _config().fingerprint


def test_embed_single_and_batch():
    body = json.dumps(
        {
            "data": [
                {"index": 1, "embedding": [0.4, 0.3, 0.2, 0.1]},
                {"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]},
            ]
        }
    ).encode()
    opener, calls = _fake_opener([(200, body), (200, body)])
    client = EmbeddingClient(_config(), opener=opener)
    results = client.embed(["first text", "second text"])
    assert [r.vector for r in results] == [[0.1, 0.2, 0.3, 0.4], [0.4, 0.3, 0.2, 0.1]]  # index order
    assert calls[0]["input"] == ["first text", "second text"]
    assert calls[0]["model"] == "bge-m3"
    assert results[0].vector_literal.startswith("[0.1,0.2")


def test_embed_empty_input_is_noop():
    opener, calls = _fake_opener([])
    client = EmbeddingClient(_config(), opener=opener)
    assert client.embed([]) == []
    assert calls == []


def test_dimension_mismatch_is_structured():
    body = json.dumps({"data": [{"index": 0, "embedding": [0.1, 0.2]}]}).encode()
    opener, _ = _fake_opener([(200, body)])
    client = EmbeddingClient(_config(), opener=opener)
    with pytest.raises(EmbeddingError) as excinfo:
        client.embed(["text"])
    assert excinfo.value.code == "dimension_mismatch"


def test_http_error_is_structured():
    opener, _ = _fake_opener([(500, b"boom")])
    client = EmbeddingClient(_config(), opener=opener)
    with pytest.raises(EmbeddingError) as excinfo:
        client.embed(["text"])
    assert excinfo.value.code == "request_failed"
    assert "500" in str(excinfo.value)


def test_unreachable_endpoint_is_structured():
    opener, _ = _fake_opener([OSError("no route")])
    client = EmbeddingClient(_config(), opener=opener)
    with pytest.raises(EmbeddingError) as excinfo:
        client.embed(["text"])
    assert excinfo.value.code == "request_failed"


def test_config_from_env():
    env = {
        "EMBEDDING_BASE_URL": "http://x:1/v1",
        "EMBEDDING_MODEL": "m",
        "EMBEDDING_DIM": "8",
        "EMBEDDING_API_KEY": "k",
    }
    config = EmbeddingConfig.from_env(env)
    assert config is not None and config.dim == 8 and config.api_key == "k"
    assert EmbeddingConfig.from_env({"EMBEDDING_BASE_URL": "http://x"}) is None
    assert EmbeddingConfig.from_env({"EMBEDDING_BASE_URL": "http://x", "EMBEDDING_MODEL": "m"}) is None
    assert EmbeddingConfig.from_env(
        {"EMBEDDING_BASE_URL": "http://x", "EMBEDDING_MODEL": "m", "EMBEDDING_DIM": "zero"}
    ) is None


def test_derive_status_states():
    config = _config()
    assert derive_status(None, 0, 100, 0) == "unconfigured"
    assert derive_status(config, 0, 100, 0) == "missing"
    assert derive_status(config, 50, 100, 10) == "missing"
    assert derive_status(config, 100, 100, 5) == "stale"
    assert derive_status(config, 100, 100, 0) == "ready"

def test_chunk_text_short_text_is_single_chunk():
    assert chunk_text("hello", 800, 0) == ["hello"]


def test_chunk_text_covers_whole_document():
    text = "".join(str(i % 10) for i in range(2000))
    chunks = chunk_text(text, 800, 0)
    assert "".join(chunks) == text  # no character dropped
    assert all(len(chunk) <= 800 for chunk in chunks)
    assert len(chunks) == 3


def test_chunk_text_overlap_windows():
    text = "".join(str(i % 10) for i in range(1600))
    chunks = chunk_text(text, 800, 200)
    assert len(chunks) == 3
    # consecutive windows share the 200-char overlap
    assert chunks[0][600:] == chunks[1][:200]
    assert chunks[1][600:] == chunks[2][:200]


def test_chunk_text_invalid_parameters():
    with pytest.raises(ValueError):
        chunk_text("x", 0, 0)
    with pytest.raises(ValueError):
        chunk_text("x", 100, 100)
    with pytest.raises(ValueError):
        chunk_text("x", 100, -1)


def test_fingerprint_covers_chunking_parameters():
    base = EmbeddingConfig(base_url="http://e:1/v1", model="m", dim=8)
    other_chunk_chars = EmbeddingConfig(base_url="http://e:1/v1", model="m", dim=8, chunk_chars=400)
    other_overlap = EmbeddingConfig(base_url="http://e:1/v1", model="m", dim=8, chunk_overlap=100)
    other_tokens = EmbeddingConfig(base_url="http://e:1/v1", model="m", dim=8, max_model_tokens=256)
    assert base.fingerprint != other_chunk_chars.fingerprint
    assert base.fingerprint != other_overlap.fingerprint
    assert base.fingerprint != other_tokens.fingerprint
    assert base.fingerprint == EmbeddingConfig(base_url="http://e:1/v1", model="m", dim=8).fingerprint
