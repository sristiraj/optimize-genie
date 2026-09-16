"""Load-test router — concurrent Genie latency benchmarking.

POST /api/spaces/{space_id}/load-test  (SSE)

Fires `concurrent_users` parallel Genie start-conversation requests, streams
per-request progress events, then emits a latency-percentile summary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.services.auth import get_workspace_client, run_in_context
from backend.services.genie_client import get_serialized_space

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_QUESTIONS = [
    "How many records are in this dataset?",
    "What are the most recent entries?",
    "Show me a summary of the data.",
    "What are the top 5 values?",
    "How has the data changed over the last month?",
]

_MAX_CONCURRENT = 50
_REQUEST_TIMEOUT = 120

# Substrings that indicate a rate-limit / capacity / busy response. Genie may
# surface these either as an HTTP error (429/503), a FAILED message error, or —
# critically — as a COMPLETED message whose content is a "busy" apology. All of
# these are reclassified as `rate_limited` and counted as failures, never
# successes, so load-test success rates aren't inflated by throttled requests.
_RATE_LIMIT_ERROR_MARKERS = (
    "429", "too many requests", "rate limit", "rate-limit", "ratelimit",
    "resource_exhausted", "resource exhausted", "quota", "throttl",
    "overloaded", "at capacity", "temporarily unavailable",
    "service unavailable", "503", "high demand", "try again later",
)
# Narrower, high-confidence markers for scanning COMPLETED message content, to
# avoid misclassifying a legitimate answer that merely mentions "busy".
_BUSY_CONTENT_MARKERS = (
    "experiencing high demand", "currently busy", "server is busy",
    "too many requests", "rate limit", "at capacity", "overloaded",
    "temporarily unavailable", "try again later", "429",
)


def _looks_rate_limited(text: str, markers: tuple[str, ...]) -> bool:
    t = (text or "").lower()
    return any(m in t for m in markers)


def _extract_message_text(msg: dict) -> str:
    """Concatenate visible text from a Genie message (attachments + content)."""
    parts: list[str] = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        txt = att.get("text")
        if isinstance(txt, dict):
            parts.append(str(txt.get("content") or ""))
        elif isinstance(txt, str):
            parts.append(txt)
    if msg.get("content"):
        parts.append(str(msg.get("content")))
    return " ".join(parts)


class LoadTestRequest(BaseModel):
    # Stepped ramp: run sequential waves from start_users up to max_users,
    # increasing by `step` each wave. When start == max, it's a single wave.
    start_users: int = Field(default=2, ge=1, le=_MAX_CONCURRENT)
    max_users: int = Field(default=10, ge=1, le=_MAX_CONCURRENT)
    step: int = Field(default=2, ge=1, le=_MAX_CONCURRENT)
    question: str | None = Field(default=None, max_length=500)


def _ramp_levels(start: int, maximum: int, step: int) -> list[int]:
    """Concurrency levels for the ramp, e.g. (2, 10, 2) → [2, 4, 6, 8, 10]."""
    start = max(1, min(start, _MAX_CONCURRENT))
    maximum = max(start, min(maximum, _MAX_CONCURRENT))
    step = max(1, step)
    levels = list(range(start, maximum + 1, step))
    if not levels:
        levels = [start]
    if levels[-1] != maximum:
        levels.append(maximum)
    return levels


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _pct(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = max(0, int(len(sorted_data) * p / 100) - 1)
    return round(sorted_data[min(idx, len(sorted_data) - 1)])


def _summarize(results: list[dict], concurrent_users: int, step_index: int) -> dict:
    """Latency-percentile summary for one wave."""
    latencies = [r["latency_ms"] for r in results if r["status"] == "success"]
    return {
        "step_index": step_index,
        "concurrent_users": concurrent_users,
        "total": len(results),
        "success": sum(1 for r in results if r["status"] == "success"),
        "error": sum(1 for r in results if r["status"] == "error"),
        "timeout": sum(1 for r in results if r["status"] == "timeout"),
        "rate_limited": sum(1 for r in results if r["status"] == "rate_limited"),
        "avg_ms": round(statistics.mean(latencies)) if latencies else 0,
        "p50_ms": _pct(latencies, 50),
        "p95_ms": _pct(latencies, 95),
        "p99_ms": _pct(latencies, 99),
        "min_ms": round(min(latencies)) if latencies else 0,
        "max_ms": round(max(latencies)) if latencies else 0,
    }


def _as_text(val) -> str:
    """Coerce a Genie config field to a plain string.

    Many serialized_space fields (e.g. benchmark ``question``) are list-valued
    (``["Show top 10..."]``); join list elements and strip. Handles str, list,
    None, and other scalars without raising.
    """
    if isinstance(val, list):
        return " ".join(str(x) for x in val).strip()
    if val is None:
        return ""
    return str(val).strip()


def _pick_questions(space_config: dict, n: int, custom: str | None) -> list[str]:
    """Return n questions to use for load testing."""
    if custom:
        return [custom] * n

    # Pull native benchmark questions from the space config. Question fields may
    # be strings or lists depending on the source, so normalize each one.
    native: list[str] = []
    benchmarks = space_config.get("benchmarks", {}) or {}
    questions = benchmarks.get("questions", []) if isinstance(benchmarks, dict) else []
    for q in questions:
        if isinstance(q, str):
            text = q.strip()
        elif isinstance(q, dict):
            text = _as_text(q.get("question") or q.get("text"))
        else:
            text = ""
        if text:
            native.append(text)

    pool = native if native else _DEFAULT_QUESTIONS
    # Cycle through the pool to fill n slots
    return [pool[i % len(pool)] for i in range(n)]


async def _single_request(
    ws,
    space_id: str,
    question: str,
    request_id: int,
) -> dict:
    """Run one Genie conversation and return timing + outcome."""
    loop = asyncio.get_event_loop()
    start = time.monotonic()

    def _call():
        poll_interval = 2.0
        try:
            resp = ws.api_client.do(
                "POST",
                f"/api/2.0/genie/spaces/{space_id}/start-conversation",
                body={"content": question},
            )
            conv_id = resp["conversation"]["id"]
            msg_id = resp["message"]["id"]
            t_first_token = time.monotonic()

            deadline = start + _REQUEST_TIMEOUT
            while True:
                if time.monotonic() > deadline:
                    return {"status": "timeout", "t_first_token": None}
                msg = ws.api_client.do(
                    "GET",
                    f"/api/2.0/genie/spaces/{space_id}/conversations/{conv_id}/messages/{msg_id}",
                )
                status = msg.get("status", "")
                if status == "COMPLETED":
                    # A 200/COMPLETED response can still be a "busy" apology when
                    # the agent is throttled — treat those as rate-limited, not
                    # successful.
                    content = _extract_message_text(msg)
                    if _looks_rate_limited(content, _BUSY_CONTENT_MARKERS):
                        return {
                            "status": "rate_limited",
                            "t_first_token": t_first_token,
                            "detail": "Genie returned a busy/high-demand response",
                        }
                    return {"status": "success", "t_first_token": t_first_token}
                if status in ("FAILED", "CANCELLED"):
                    err = msg.get("error")
                    err_text = (
                        err if isinstance(err, str)
                        else err.get("message", "") if isinstance(err, dict)
                        else ""
                    )
                    st = "rate_limited" if _looks_rate_limited(err_text, _RATE_LIMIT_ERROR_MARKERS) else "error"
                    return {"status": st, "t_first_token": t_first_token, "detail": (err_text or status)[:160]}
                time.sleep(poll_interval)
        except Exception as exc:
            text = str(exc)
            st = "rate_limited" if _looks_rate_limited(text, _RATE_LIMIT_ERROR_MARKERS) else "error"
            return {"status": st, "t_first_token": None, "detail": text[:160]}

    result = await loop.run_in_executor(None, _call)
    end = time.monotonic()

    return {
        "request_id": request_id,
        "question": question[:80],
        "status": result["status"],
        "latency_ms": round((end - start) * 1000),
        "ttfr_ms": round((result["t_first_token"] - start) * 1000) if result.get("t_first_token") else None,
        "detail": result.get("detail"),
    }


@router.post("/api/spaces/{space_id}/load-test")
async def run_load_test(space_id: str, body: LoadTestRequest, request: Request):
    """Stream SSE load-test progress then emit a latency summary."""
    ws = get_workspace_client()
    # Capture the user token so the streaming generator can re-establish the OBO
    # context (ContextVars don't propagate into async generators that outlive the
    # middleware's call_next). main.py stores it as request.state.user_token.
    user_token = getattr(request.state, "user_token", "")

    # Capture GSO config on the request thread — the ContextVar is not available
    # inside the streaming generator / executor, so resolve it here and pass it
    # explicitly to the Delta writer.
    from backend.services.request_config import get_request_config
    rc = get_request_config()
    gso_catalog = rc.gso_catalog or os.environ.get("GSO_CATALOG", "")
    gso_schema = os.environ.get("GSO_SCHEMA", "genie_space_optimizer")
    gso_warehouse = (
        rc.warehouse_id
        or os.environ.get("GSO_WAREHOUSE_ID")
        or os.environ.get("SQL_WAREHOUSE_ID", "")
    )

    async def stream() -> AsyncIterator[str]:
        # Re-set OBO context inside the generator (streaming path)
        if user_token:
            from backend.services.auth import set_obo_user_token
            set_obo_user_token(user_token)

        try:
            # Fetch space config to pull native benchmark questions
            try:
                space_config = await asyncio.get_event_loop().run_in_executor(
                    None, run_in_context(get_serialized_space, space_id)
                )
            except Exception:
                space_config = {}

            run_id = uuid.uuid4().hex
            started_dt = datetime.now(timezone.utc)
            started_at = started_dt.isoformat()

            levels = _ramp_levels(body.start_users, body.max_users, body.step)
            total_requests = sum(levels)
            yield _sse("start", {
                "run_id": run_id,
                "started_at": started_at,
                "levels": levels,
                "total_levels": len(levels),
                "total_requests": total_requests,
                "space_id": space_id,
            })

            step_summaries: list[dict] = []
            # Run each concurrency level as a sequential wave so latency
            # degradation under increasing load is visible step by step.
            for step_index, level in enumerate(levels):
                questions = _pick_questions(space_config, level, body.question)
                yield _sse("step_start", {
                    "step_index": step_index,
                    "concurrent_users": level,
                    "total_levels": len(levels),
                })

                tasks = [
                    _single_request(ws, space_id, questions[i], i + 1)
                    for i in range(level)
                ]

                results: list[dict] = []
                completed = 0
                for coro in asyncio.as_completed(tasks):
                    result = await coro
                    results.append(result)
                    completed += 1
                    yield _sse("progress", {
                        "step_index": step_index,
                        "concurrent_users": level,
                        "completed": completed,
                        "total": level,
                        "result": result,
                    })

                summary = _summarize(results, level, step_index)
                step_summaries.append(summary)
                yield _sse("step_summary", summary)

            finished_dt = datetime.now(timezone.utc)
            finished_at = finished_dt.isoformat()
            duration_ms = round((finished_dt - started_dt).total_seconds() * 1000)

            # Persist the run to Delta (best-effort; never breaks the stream).
            try:
                from backend.services import delta_store
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: delta_store.save_load_test(
                        run_id=run_id,
                        space_id=space_id,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=duration_ms,
                        question=body.question,
                        steps=step_summaries,
                        catalog=gso_catalog,
                        schema=gso_schema,
                        warehouse=gso_warehouse,
                    ),
                )
            except Exception:
                logger.warning("Failed to persist load test %s to Delta", run_id, exc_info=True)

            yield _sse("done", {
                "steps": step_summaries,
                "run_id": run_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
            })

        except asyncio.CancelledError:
            yield _sse("error", {"message": "Load test cancelled"})
        except Exception as exc:
            logger.exception("Load test failed for space %s", space_id)
            yield _sse("error", {"message": str(exc)[:200]})

    return StreamingResponse(stream(), media_type="text/event-stream")
