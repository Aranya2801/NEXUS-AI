from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Tolerant parser for LLM output: strips code fences and surrounding prose. Returns None on failure."""
    t = _FENCE.sub("", text.strip())
    for candidate in (t, t[t.find("{") : t.rfind("}") + 1] if "{" in t else ""):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None
