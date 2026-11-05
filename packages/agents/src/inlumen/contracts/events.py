"""The event both agents report for each model request.

``inlumen.design.run.design`` and ``inlumen.codegen.run.generate`` call an
:data:`EventSink` once per request with :func:`model_request_event`'s dict.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Receives one event dict per model request.
EventSink = Callable[[dict[str, Any]], None]


def model_request_event(
    *,
    purpose: str,
    model: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
    latency_s: float = 0.0,
    finish_reason: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """One model request: tokens (0 when not reported), cost (None when unknown), outcome."""
    return {
        "type": "model_request",
        "purpose": purpose,
        "model": model,
        "prompt_tokens": prompt_tokens or 0,
        "completion_tokens": completion_tokens or 0,
        "cost_usd": cost_usd,
        "latency_s": round(latency_s, 3),
        "finish_reason": finish_reason,
        "error": error,
    }
