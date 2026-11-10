FROM python:3.12.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY mcp_server ./mcp_server
RUN pip install --no-cache-dir ".[mcp]"

RUN useradd --system --uid 10002 --no-create-home --shell /usr/sbin/nologin mcpserver
USER 10002:10002

EXPOSE 8090

CMD ["python", "-m", "mcp_server"]
