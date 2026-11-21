from __future__ import annotations

from typing import Protocol


class Reranker(Protocol):
    name: str

    async def score(self, query: str, texts: list[str]) -> list[float]:
        """Relevance of each text to the query, each in [0, 1]. Higher is better."""
