ARG BASE_IMAGE=lmsysorg/sglang:v0.5.21
FROM ${BASE_IMAGE}

# In-stack embedding server (change add-rag-fixture-and-embed-tool): SGLang
# serving BAAI/bge-large-en-v1.5 (dim 1024) behind the OpenAI-compatible
# /v1/embeddings API. The model weights are baked at build time so the runtime
# stack (internal network, no egress) never contacts Hugging Face.
#
# The entrypoint detects at RUNTIME what the container got: GPU access (via
# docker-compose.embed-gpu.yml, `--gpus all`, or a default nvidia runtime),
# no GPU, or broken CUDA — and launches SGLang on CUDA or CPU accordingly.
# The same image therefore runs on GPU hosts, GPU-less hosts, and hosts with
# CUDA problems. CPU inference of this 335M-parameter embedding model is slow
# but functional; for heavy use, point EMBEDDING_BASE_URL at a GPU endpoint.
ARG MODEL_ID=BAAI/bge-large-en-v1.5

RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_ID}')"

ENV HF_HUB_OFFLINE=1 \
    MODEL_ID=${MODEL_ID}

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 30000

ENTRYPOINT ["python", "/opt/entrypoint.py"]
