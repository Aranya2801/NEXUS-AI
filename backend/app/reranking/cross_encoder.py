"""Cross-encoder reranker (sentence-transformers). UNTESTED here: needs `pip install sentence-transformers`
and a model download. Scores are squashed to [0,1] with a sigmoid."""
from __future__ import annotations

import asyncio
import math

from app.core.exceptions import ConfigurationError


class CrossEncoderReranker:
    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise ConfigurationError("pip install sentence-transformers to use the cross_encoder reranker") from exc
        self._model = CrossEncoder(model_name)
        self.name = f"cross-encoder:{model_name}"

    async def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        raw = await asyncio.to_thread(self._model.predict, [(query, t) for t in texts])
        return [1 / (1 + math.exp(-float(x))) for x in raw]
