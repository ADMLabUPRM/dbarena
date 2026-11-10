FROM python:3.12.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[mcp]"

COPY gui/backend/app ./gui/backend/app
COPY db/seed_tasks.py db/smoke_test.py db/reembed.py /opt/db/

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin dbarena
USER 10001:10001

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "gui.backend.app.entry:app", "--host", "0.0.0.0", "--port", "8000"]
