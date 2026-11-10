"""The judge service: a tiny LLM-backed evaluation agent with two reward modes.

Inspired by TarzanAI's LLM patterns (OpenAI-compatible chat endpoint resolved
from environment variables, temperature 0). Two evaluation modes:

- ``sparse`` (default): one holistic call — pass/fail, nothing else crosses the
  boundary.
- ``dense``: the task declares ``key_points`` (short factual requirements); each
  key point is judged in its own call for semantic conveyance (wording does not
  matter) and the response carries the graded outcome
  ``{"pass": score == 1.0, "score": satisfied/total, "satisfied": n, "total": m}``.
"""

import os
import re
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException
from langchain_openai import ChatOpenAI
from langsmith import traceable
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

app = FastAPI(title="DBArena answer judge")


class JudgeRequest(BaseModel):
    goal: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    submitted: str = Field(min_length=1)
    key_points: list[str] | None = None
    reward_mode: str | None = None


def _env_config() -> dict[str, str]:
    return {
        "base_url": (os.environ.get("JUDGE_LLM_BASE_URL") or "http://host.docker.internal:30000/v1").rstrip("/"),
        "model": os.environ.get("JUDGE_LLM_MODEL") or "glm-5.2-fp8",
        "api_key": os.environ.get("JUDGE_LLM_API_KEY") or "test",
    }


def _config() -> dict[str, str]:
    """Resolve the judge LLM config: admindb `judge_config` first, env fallback.

    The GUI edits the persisted row through the backend; the env values are the
    bootstrap defaults. Unreachable admin database degrades to env silently.
    """
    config = _env_config()
    admin_url = os.environ.get("ADMINDB_URL", "")
    if not admin_url:
        return config
    try:
        connection = psycopg.connect(
            admin_url.replace("postgresql+psycopg://", "postgresql://"), row_factory=dict_row
        )
        try:
            row = connection.execute(
                "SELECT base_url, model, api_key FROM judge_config WHERE id"
            ).fetchone()
        finally:
            connection.close()
        if row is not None and row["base_url"] and row["model"]:
            return {
                "base_url": str(row["base_url"]).rstrip("/"),
                "model": str(row["model"]),
                "api_key": str(row["api_key"] or ""),
            }
    except Exception:
        pass
    return config


def _endpoint(base_url: str) -> str:
    return f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"


_SPARSE_SYSTEM_PROMPT = (
    "You are a strict evaluation judge for a benchmark. You receive the task goal, the expected "
    "result, and a submitted answer. Judge the SUBSTANCE of the submitted answer against the "
    "expected result: every key fact, piece of reasoning, and required element (such as citations) "
    "must be conveyed, and nothing may contradict the expected result. Do NOT judge wording, "
    "phrasing, structure, or length — an answer that conveys the same facts and citations in "
    "different words PASSES. FAIL only when a required element is missing, unsupported, or "
    "contradicted. Reply with exactly one word: PASS or FAIL."
)

_DENSE_POINT_PROMPT = (
    "You are a strict evaluation judge for a benchmark. You receive one requirement from a task's "
    "expected result and the submitted answer. Decide whether the submitted answer conveys this "
    "requirement: the substance must be present (explicitly or unambiguously implied) and must not "
    "be contradicted. Do NOT judge wording, phrasing, structure, or length — conveying the same "
    "fact in different words counts. Reply with exactly one word: YES or NO."
)


def _llm_base_url(base_url: str) -> str:
    """Base URL for ChatOpenAI: the SDK appends ``/chat/completions`` itself."""
    return base_url if base_url.endswith("/v1") else f"{base_url}/v1"


@traceable(name="judge_llm_call", run_type="llm")
def _chat_completion(goal: str, expected: str, submitted: str, requirement: str | None) -> str:
    config = _config()
    if requirement is not None:
        system_prompt = _DENSE_POINT_PROMPT
        user_content = (
            f"Task goal:\n{goal}\n\nExpected result (context):\n{expected}\n\n"
            f"Submitted answer:\n{submitted}\n\n"
            f"Requirement to evaluate:\n{requirement}\n\nReply with exactly YES or NO."
        )
    else:
        system_prompt = _SPARSE_SYSTEM_PROMPT
        user_content = (
            f"Task goal:\n{goal}\n\nExpected result:\n{expected}\n\n"
            f"Submitted answer:\n{submitted}\n\nReply with exactly PASS or FAIL."
        )
    llm = ChatOpenAI(
        model=config["model"],
        temperature=0,
        api_key=config["api_key"] or "test",
        base_url=_llm_base_url(config["base_url"]),
        timeout=120,
    )
    try:
        response = llm.invoke(
            [
                ("system", system_prompt),
                ("user", user_content),
            ]
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"judge LLM unreachable: {exc}") from None
    content = response.content
    if isinstance(content, list):
        content = "".join(str(part) for part in content)
    return str(content)


def _parse_verdict(reply: str) -> bool:
    normalized = re.sub(r"[^A-Z]", "", reply.upper())
    if "PASS" in normalized and "FAIL" not in normalized:
        return True
    if "FAIL" in normalized and "PASS" not in normalized:
        return False
    raise HTTPException(status_code=502, detail=f"judge LLM reply did not parse as PASS or FAIL: {reply[:120]!r}")


def _parse_conveyed(reply: str) -> bool:
    normalized = re.sub(r"[^A-Z]", "", reply.upper())
    if "YES" in normalized and "NO" not in normalized:
        return True
    if "NO" in normalized and "YES" not in normalized:
        return False
    raise HTTPException(status_code=502, detail=f"judge LLM reply did not parse as YES or NO: {reply[:120]!r}")


@app.get("/health")
def health() -> dict[str, Any]:
    config = _config()
    return {
        "status": "ok",
        "model": config["model"],
        "endpoint": _endpoint(config["base_url"]),
        "tracing": os.environ.get("LANGSMITH_TRACING", "").lower() == "true",
    }


@traceable(name="judge_evaluation")
@app.post("/judge")
def judge(request: JudgeRequest) -> dict[str, Any]:
    if request.reward_mode == "dense":
        if not request.key_points:
            raise HTTPException(status_code=400, detail="dense reward_mode requires key_points")
        satisfied = 0
        for key_point in request.key_points:
            reply = _chat_completion(request.goal, request.expected, request.submitted, key_point)
            if _parse_conveyed(reply):
                satisfied += 1
        score = satisfied / len(request.key_points)
        return {"pass": score == 1.0, "score": score, "satisfied": satisfied, "total": len(request.key_points)}
    reply = _chat_completion(request.goal, request.expected, request.submitted, None)
    return {"pass": _parse_verdict(reply)}
