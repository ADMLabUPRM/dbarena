"""Client for the answer-judge service.

The environment receives a judge resolver callable (built here from
``JUDGE_URL``) and calls it at judged submissions: goal + authored expected
result + submitted answer → an evaluation outcome. Sparse tasks get a binary
verdict; dense tasks (which declare key points and ``reward_mode: "dense"``)
get a graded outcome ``(passed, score, satisfied, total)``. Infrastructure
problems (unreachable service, non-200, malformed verdict) raise
:class:`JudgeError`, which the environment surfaces as a structured error while
keeping the episode open.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

JUDGE_TIMEOUT_SECONDS = 60.0


class JudgeError(Exception):
    """Judge infrastructure problem: unreachable, error status, or malformed verdict."""


@dataclass(frozen=True)
class JudgeOutcome:
    passed: bool
    score: float = 1.0
    satisfied: int = 1
    total: int = 1


def post_verdict(
    base_url: str,
    goal: str,
    expected: str,
    submitted: str,
    key_points: Sequence[str] | None = None,
    reward_mode: str = "sparse",
    timeout: float = JUDGE_TIMEOUT_SECONDS,
) -> JudgeOutcome:
    url = base_url.rstrip("/") + "/judge"
    payload: dict[str, Any] = {"goal": goal, "expected": expected, "submitted": submitted}
    if key_points:
        payload["key_points"] = list(key_points)
    if reward_mode != "sparse":
        payload["reward_mode"] = reward_mode
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise JudgeError(f"judge service returned HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise JudgeError(f"judge service unreachable: {exc}") from None
    verdict = body.get("pass") if isinstance(body, dict) else None
    if not isinstance(verdict, bool):
        raise JudgeError(f"judge service returned a malformed verdict: {body!r}"[:200])
    if key_points and reward_mode == "dense":
        score = body.get("score")
        satisfied = body.get("satisfied")
        total = body.get("total")
        if not all(isinstance(value, (int, float)) for value in (score, satisfied, total)):
            raise JudgeError("judge service returned a malformed dense verdict")
        return JudgeOutcome(passed=verdict, score=float(score), satisfied=int(satisfied), total=int(total))
    return JudgeOutcome(passed=verdict)


def build_judge_resolver(
    judge_url: str | None,
) -> Callable[[str, str, str, Sequence[str] | None, str], JudgeOutcome] | None:
    """Build the judge resolver for the environment; None when unconfigured."""
    if not judge_url or not judge_url.strip():
        return None

    def resolve(
        goal: str, expected: str, submitted: str, key_points: Sequence[str] | None = None, reward_mode: str = "sparse"
    ) -> JudgeOutcome:
        return post_verdict(judge_url, goal, expected, submitted, key_points, reward_mode)

    return resolve
