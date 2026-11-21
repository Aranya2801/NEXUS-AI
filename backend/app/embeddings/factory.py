from __future__ import annotations

from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from app.embeddings.base import Embedder
from app.embeddings.hashing import HashingEmbedder


def build_embedder(settings: Settings) -> Embedder:
    p = settings.embedding_provider
    if p == "mock":
        return HashingEmbedder(settings.embedding_dim)
    if p == "openai":
        from app.embeddings.openai_compat import OpenAICompatEmbedder

        return OpenAICompatEmbedder(model=settings.embedding_model, dim=settings.embedding_dim,
                                    api_key=settings.embedding_api_key, base_url=settings.embedding_base_url)
    if p == "sentence_transformers":
        from app.embeddings.sentence_transformers import SentenceTransformerEmbedder

        return SentenceTransformerEmbedder(settings.embedding_model, settings.embedding_dim)
    raise ConfigurationError(f"Unknown embedding provider '{p}'")
