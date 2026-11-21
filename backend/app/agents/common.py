from __future__ import annotations

import functools
import time

import httpx
from langgraph.errors import GraphBubbleUp

from app.core.exceptions import PermissionDenied
from app.core.logging import get_logger

log = get_logger("nexus.agents")
# Errors worth retrying (network/IO). Re-raised so LangGraph's RetryPolicy handles them.
TRANSIENT = (httpx.TransportError, ConnectionError, TimeoutError, OSError)


def traced(agent: str, node: str):
    """Times a node, appends a trace entry, and converts unexpected *non-transient* failures into state errors so one
    failing agent degrades the run instead of crashing it. Security errors and transient errors always propagate."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(state):
            t0 = time.perf_counter()
            try:
                out = await fn(state)
            except (GraphBubbleUp, PermissionDenied, *TRANSIENT):
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("agent node failed", extra={"extra_fields": {"agent": agent, "node": node}})
                out = {"errors": [f"{node}: {type(exc).__name__}: {str(exc)[:200]}"]}
            out = dict(out or {})
            entry = {"agent": agent, "node": node, "ms": round((time.perf_counter() - t0) * 1000, 2)}
            entry.update(out.pop("_trace", {}))
            out["trace"] = [*(out.get("trace") or []), entry]
            return out
        return wrapper
    return deco
