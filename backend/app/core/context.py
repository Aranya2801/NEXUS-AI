"""Request-scoped attribution (org/user) propagated through asyncio tasks so that LLM calls made deep inside workflows can be
metered per tenant without threading a principal through every signature."""
from __future__ import annotations

from contextvars import ContextVar

usage_context: ContextVar[dict | None] = ContextVar("nexus_usage_context", default=None)


def set_usage_context(org_id: str | None, user_id: str | None) -> None:
    usage_context.set({"org_id": org_id, "user_id": user_id})


def current_org() -> str | None:
    return (usage_context.get() or {}).get("org_id")


def current_user() -> str | None:
    return (usage_context.get() or {}).get("user_id")


def set_prompt_pins(pins: dict[str, str] | None) -> None:
    """Experiment assignments for the current run: {task: prompt version ("builtin" or a number)}. Read by the prompt store."""
    ctx = dict(usage_context.get() or {})
    ctx["pins"] = dict(pins or {})
    usage_context.set(ctx)


def current_pins() -> dict[str, str]:
    return (usage_context.get() or {}).get("pins") or {}
