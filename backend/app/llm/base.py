from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0


@dataclass(slots=True)
class UsageMeter:
    """Accumulates LLM usage across a pipeline run (feeds cost/latency evaluation in Phase 9)."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    by_task: dict[str, int] = field(default_factory=dict)

    def add(self, task: str, r: LLMResponse) -> None:
        self.calls += 1
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.latency_ms += r.latency_ms
        self.by_task[task] = self.by_task.get(task, 0) + 1


class LLM(Protocol):
    name: str

    async def complete(self, system: str, user: str, *, max_tokens: int = 800, temperature: float = 0.0) -> LLMResponse: ...
