"""Configure the embedding endpoint from the environment and run the RAG re-embed to readiness."""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8000/api"
TIMEOUT_SECONDS = int(os.environ.get("REEMBED_TIMEOUT", "900"))


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else b""
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main() -> int:
    config = {
        "base_url": os.environ.get("EMBEDDING_BASE_URL", ""),
        "model": os.environ.get("EMBEDDING_MODEL", ""),
        "dim": int(os.environ.get("EMBEDDING_DIM", "1024")),
        "api_key": os.environ.get("EMBEDDING_API_KEY", ""),
    }
    if not config["base_url"] or not config["model"]:
        print("[REEMBED] FAILED: EMBEDDING_BASE_URL / EMBEDDING_MODEL are not set", flush=True)
        return 1
    _call("PUT", "/embeddings/config", config)
    print(
        f"[REEMBED] config saved: {config['model']} dim={config['dim']}"
        f" -> {config['base_url']}",
        flush=True,
    )
    try:
        _call("POST", "/embeddings/reembed")
    except urllib.error.HTTPError as error:
        print(f"[REEMBED] reembed POST returned {error.code}; polling status anyway", flush=True)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        status = _call("GET", "/embeddings/status")
        state = status.get("state")
        job = status.get("job") or {}
        print(
            f"[REEMBED] state={state} embedded={status.get('embedded', 0)}/{status.get('total', 0)}"
            f" job={job.get('state')} completed={job.get('completed', 0)}",
            flush=True,
        )
        if state == "ready":
            print("[REEMBED] embedding lifecycle ready", flush=True)
            return 0
        if state == "unconfigured" or job.get("state") == "failed":
            print(f"[REEMBED] FAILED: lifecycle state={state} job={job}", flush=True)
            return 1
        if time.monotonic() >= deadline:
            print(f"[REEMBED] FAILED: not ready after {TIMEOUT_SECONDS}s", flush=True)
            return 1
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
