"""Query classification and adaptive retrieval policy.

The adaptive policy maps cheap query features to an *escalation ladder* of retrieval plans, cheapest first. The
pipeline walks the ladder only while retrieval confidence stays below the sufficiency threshold (bounded by
`max_retrieval_loops`). A `FixedPolicy` runs one plan regardless of the query; the research experiment (Phase 9)
compares the two on quality, latency and LLM cost.

The classifier is rule-based and unvalidated: its accuracy must be measured on the benchmark before any claim."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Literal

from app.retrieval.sparse import tokenize
from app.retrieval.types import Mode, Temporal

QueryClass = Literal["lookup", "factoid", "comparison", "multi_hop", "exploratory"]

_COMPARE = re.compile(r"\b(compare|comparison|versus|vs\.?|difference|differences|differ|contrast|trade-?offs?|better than)\b", re.I)
_EXPLORE = re.compile(r"\b(overview|summar\w+|survey|landscape|state of the art|trends?|gaps?|opportunit\w+|all (?:the )?\w+ (?:about|on))\b", re.I)
_MULTI = re.compile(r"\b(relationship between|connect\w*|related to|both|each of|across|and (?:what|how|which|why)|then)\b", re.I)
_QWORD = re.compile(r"\b(what|which|who|whom|how|why|when|where)\b", re.I)
_EXACT = re.compile(r"(\"[^\"]+\"|\b[A-Za-z]*\d+[A-Za-z\d_-]*\b|\b[A-Z]{2,}[-_]?\d*\b)")
_HISTORY = re.compile(r"\b(previous|previously|older|earlier|history|historical|changed?|since|before|prior|superseded|version|versions|was)\b", re.I)


@dataclass(slots=True)
class QueryFeatures:
    n_tokens: int
    n_question_words: int
    has_exact_token: bool
    comparison: bool
    exploratory: bool
    multi_part: bool
    history_cue: bool
    complexity: float  # 0..1 heuristic

    @property
    def query_class(self) -> QueryClass:
        if self.comparison:
            return "comparison"
        if self.multi_part:
            return "multi_hop"
        if self.exploratory:
            return "exploratory"
        if self.has_exact_token or (self.n_tokens <= 3 and self.n_question_words == 0):
            return "lookup"
        return "factoid"


def extract_features(query: str) -> QueryFeatures:
    toks = tokenize(query)
    qw = len(_QWORD.findall(query))
    comparison = bool(_COMPARE.search(query))
    multi = bool(_MULTI.search(query)) or qw >= 2 or len(toks) > 25
    complexity = min(1.0, 0.15 * qw + 0.25 * comparison + 0.25 * multi + 0.2 * (len(toks) > 12) + 0.1 * bool(_EXPLORE.search(query)))
    return QueryFeatures(len(toks), qw, bool(_EXACT.search(query)), comparison, bool(_EXPLORE.search(query)), multi,
                         bool(_HISTORY.search(query)), round(complexity, 3))


@dataclass(slots=True)
class RetrievalPlan:
    name: str
    mode: Mode = "hybrid"
    top_k: int = 8
    candidates: int = 40
    rerank: bool = True
    decompose: bool = False
    multi_query: int = 0
    hyde: bool = False
    compress: bool = False
    graph: bool = False          # knowledge-graph expansion (Phase 5)
    temporal: Temporal = "current"
    level: int = 0
    meta: dict = field(default_factory=dict)

    def cost_hint(self) -> int:
        """Rough relative cost: number of retrieval passes + LLM calls the plan will make."""
        passes = (self.multi_query + 1) * (3 if self.decompose else 1)
        return passes + int(self.decompose) + int(self.multi_query > 0) + int(self.hyde) + int(self.compress) * self.top_k


class FixedPolicy:
    name = "fixed"

    def __init__(self, plan: RetrievalPlan) -> None:
        self.plan = plan

    def ladder(self, query: str) -> list[RetrievalPlan]:
        return [self.plan]


class AdaptivePolicy:
    name = "adaptive"

    def ladder(self, query: str) -> list[RetrievalPlan]:
        f = extract_features(query)
        cls = f.query_class
        temporal: Temporal = "all_versions" if f.history_cue else "current"
        base = RetrievalPlan(name=cls, temporal=temporal)
        if cls == "lookup":
            plans = [replace(base, mode="sparse", rerank=False),
                     replace(base, rerank=True, level=1),
                     replace(base, rerank=True, multi_query=3, level=2)]
        elif cls == "factoid":
            plans = [replace(base),
                     replace(base, multi_query=3, level=1),
                     replace(base, multi_query=3, hyde=True, candidates=60, level=2)]
        elif cls == "comparison":
            plans = [replace(base, decompose=True, top_k=10),
                     replace(base, decompose=True, multi_query=2, top_k=12, level=1),
                     replace(base, decompose=True, multi_query=3, hyde=True, top_k=12, candidates=60, level=2)]
        elif cls == "multi_hop":
            plans = [replace(base, decompose=True, top_k=10, graph=True),
                     replace(base, decompose=True, multi_query=2, top_k=12, graph=True, level=1),
                     replace(base, decompose=True, multi_query=3, hyde=True, top_k=12, candidates=60, graph=True, level=2)]
        else:  # exploratory
            plans = [replace(base, multi_query=3, top_k=12),
                     replace(base, multi_query=3, decompose=True, top_k=14, level=1),
                     replace(base, multi_query=3, decompose=True, hyde=True, top_k=14, candidates=60, level=2)]
        for p in plans:
            p.meta["features"] = {"class": cls, "complexity": f.complexity}
        return plans


class GraphAdaptivePolicy(AdaptivePolicy):
    """Adaptive ladder with knowledge-graph expansion switched on at every level (experiment system S6)."""
    name = "adaptive_graph"

    def ladder(self, query: str) -> list[RetrievalPlan]:
        return [replace(p, graph=True) for p in super().ladder(query)]
