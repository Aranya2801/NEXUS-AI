from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    dim: int
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
