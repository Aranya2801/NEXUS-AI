"""Local sentence-transformers embedder. UNTESTED in CI (needs `pip install sentence-transformers` and a
model download); keep it behind the factory so offline mode never imports torch."""
from __future__ import annotations

import asyncio

from app.core.exceptions import ConfigurationError


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, dim: int) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ConfigurationError("pip install sentence-transformers to use this provider") from exc
        self._model = SentenceTransformer(model_name)
        actual = self._model.get_sentence_embedding_dimension()
        if actual != dim:
            raise ConfigurationError(f"Model dim {actual} != NEXUS_EMBEDDING_DIM {dim}")
        self.dim, self.name = dim, f"st:{model_name}:{dim}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = await asyncio.to_thread(self._model.encode, texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]
