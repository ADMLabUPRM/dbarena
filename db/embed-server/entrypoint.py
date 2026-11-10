"""In-container entrypoint for the SGLang embedding server.

Detects what the container actually got at runtime — GPU access (granted by
the runtime/overlay), or none — and launches SGLang accordingly. CUDA problems
(driver mismatch, broken toolkit) degrade to CPU instead of crashing, so the
same image runs on GPU hosts, GPU-less hosts, and hosts with broken CUDA.

Set SGLANG_DEVICE=cpu|cuda to skip detection explicitly.
"""

import os
import shutil
import subprocess
import sys
import time


def _detect_device() -> str:
    forced = os.environ.get("SGLANG_DEVICE", "").strip().lower()
    if forced in ("cpu", "cuda"):
        print(f"[EMBED] device forced via SGLANG_DEVICE={forced}", flush=True)
        return forced
    if shutil.which("nvidia-smi") is None:
        print("[EMBED] no NVIDIA driver visible inside the container; using CPU", flush=True)
        return "cpu"
    try:
        probe = subprocess.run(
            ["nvidia-smi"], capture_output=True, timeout=15, check=False
        )
        if probe.returncode != 0:
            print("[EMBED] nvidia-smi failed; using CPU", flush=True)
            return "cpu"
    except (OSError, subprocess.TimeoutExpired):
        print("[EMBED] nvidia-smi could not run; using CPU", flush=True)
        return "cpu"
    try:
        import torch

        if not torch.cuda.is_available():
            print(
                "[EMBED] NVIDIA hardware visible but CUDA is not usable "
                f"({torch.cuda.verification_error() if hasattr(torch.cuda, 'verification_error') else 'unavailable'}); "
                "using CPU",
                flush=True,
            )
            return "cpu"
        name = torch.cuda.get_device_name(0)
        print(f"[EMBED] CUDA available: {name}; using GPU", flush=True)
        return "cuda"
    except Exception as exc:  # noqa: BLE001 - any CUDA problem degrades to CPU
        print(f"[EMBED] CUDA check failed ({exc}); using CPU", flush=True)
        return "cpu"


def main() -> int:
    device = _detect_device()
    # The triton attention backend has no CPU driver: with it, the prefill
    # allocator crashes on the first warmup batch ("0 active drivers") and the
    # container crash-loops. torch_native serves embeddings on CPU (verified
    # live); triton stays the CUDA choice (verified live for BERT-arch models).
    backend = "torch_native" if device == "cpu" else "triton"
    args = [
        sys.executable,
        "-m",
        "sglang.launch_server",
        "--model-path",
        os.environ.get("MODEL_ID", "BAAI/bge-large-en-v1.5"),
        "--is-embedding",
        "--host",
        "0.0.0.0",
        "--port",
        os.environ.get("SGLANG_PORT", "30000"),
        "--mem-fraction-static",
        os.environ.get("SGLANG_MEM_FRACTION", "0.5"),
        # SGLang embedding-model requirements (verified live): the overlap
        # scheduler corrupts embedding outputs (non-deterministic vectors),
        # and CUDA graphs + the flashinfer backend degrade BERT-arch pooling.
        "--disable-overlap-schedule",
        "--disable-radix-cache",
        "--disable-cuda-graph",
        "--attention-backend",
        backend,
    ]
    if device == "cpu":
        args += ["--device", "cpu"]
    print(f"[EMBED] launching: {' '.join(args)}", flush=True)
    for attempt in range(2):
        outcome = subprocess.run(args, check=False)
        if outcome.returncode == 0:
            return 0
        if device == "cuda" and attempt == 0:
            print("[EMBED] CUDA launch failed; retrying once on CPU", flush=True)
            args[args.index("--attention-backend") + 1] = "torch_native"
            args += ["--device", "cpu"]
            device = "cpu"
            time.sleep(2)
            continue
        return outcome.returncode
    return 1


if __name__ == "__main__":
    sys.exit(main())
