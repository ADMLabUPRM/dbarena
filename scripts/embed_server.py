"""Start the in-stack SGLang embedding server, adapting to the host.

Detection happens in two layers so `docker compose up` works on any host:

1. Host layer (this script / the make target): if the host has a working
   NVIDIA GPU, the GPU overlay (docker-compose.embed-gpu.yml) is included so
   the container receives GPU access; otherwise the container starts without
   a GPU grant. If the GPU variant cannot start (no nvidia runtime, broken
   CUDA, no GPU), the CPU variant is started automatically.
2. Container layer (db/embed-server/entrypoint.py): the entrypoint re-checks
   inside the container (nvidia-smi + torch.cuda.is_available) and launches
   SGLang on CUDA or CPU accordingly — so the same image also behaves when
   built and run directly on other hosts, with or without GPU access.
"""

import os
import subprocess
import sys

COMPOSE_FILES = ["-f", "docker-compose.yml", "-f", "docker-compose.embed-gpu.yml"]
PROFILE = ["--profile", "embed"]
SERVICE = "embed-server"


def host_has_gpu() -> bool:
    try:
        probe = subprocess.run(["nvidia-smi"], capture_output=True, timeout=15, check=False)
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def compose_start(extra_files: list[str]) -> int:
    cmd = ["docker", "compose", *extra_files, *PROFILE, "up", "-d", SERVICE]
    print(">", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=False).returncode


def main() -> int:
    files = ["-f", "docker-compose.yml"]
    if host_has_gpu():
        print("[EMBED] NVIDIA GPU detected on the host; requesting GPU access.", flush=True)
        if compose_start(COMPOSE_FILES) == 0:
            print("[EMBED] embed-server started with GPU access.", flush=True)
            print("Follow the model load: docker compose logs -f embed-server", flush=True)
            return 0
        print(
            "[EMBED] GPU variant failed to start (missing nvidia runtime or broken CUDA); "
            "falling back to CPU.",
            flush=True,
        )
    else:
        print(
            "[EMBED] No NVIDIA GPU detected on the host; starting the CPU variant. "
            "CPU embedding works but is slow — an external GPU endpoint can be "
            "configured from the admin GUI instead.",
            flush=True,
        )
    code = compose_start(files)
    if code == 0:
        print("[EMBED] embed-server started (CPU mode).", flush=True)
        print("Follow the model load: docker compose logs -f embed-server", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
