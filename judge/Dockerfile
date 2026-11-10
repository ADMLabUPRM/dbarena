FROM python:3.12-slim

# The answer judge (change add-rag-judged-task): a tiny LLM-backed evaluation
# service. Given the task goal, the authored expected result, and the agent's
# final submission, it returns exactly {"pass": true|false} — no feedback
# crosses the boundary. The LLM endpoint is OpenAI-compatible and configured
# through JUDGE_LLM_* environment variables (see docker-compose.yml).
RUN pip install --no-cache-dir "fastapi==0.142.2" "uvicorn[standard]==0.54.0" "psycopg[binary]==3.2.13" "langsmith==0.14.7" "langchain-openai==0.3.35"

COPY app.py /opt/app.py

WORKDIR /opt

EXPOSE 9100

ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9100"]
