from __future__ import annotations

from app.core.config import Settings
from app.reranking.base import Reranker
from app.reranking.lexical import LexicalReranker


def build_reranker(settings: Settings) -> Reranker:
    if settings.reranker_provider == "cross_encoder":
        from app.reranking.cross_encoder import CrossEncoderReranker

        return CrossEncoderReranker(settings.reranker_model)
    return LexicalReranker()
