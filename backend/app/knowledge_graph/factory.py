from __future__ import annotations

from app.core.config import Settings
from app.knowledge_graph.llm_extractor import LLMExtractor
from app.knowledge_graph.rule_extractor import RuleBasedExtractor
from app.llm.base import LLM


def build_extractor(settings: Settings, llm: LLM):
    return LLMExtractor(llm) if settings.graph_extractor == "llm" else RuleBasedExtractor()
