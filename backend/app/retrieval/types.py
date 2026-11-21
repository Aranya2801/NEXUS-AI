from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Mode = Literal["dense", "sparse", "hybrid"]
Temporal = Literal["current", "as_of", "all_versions"]


@dataclass(slots=True)
class SearchFilters:
    collection_ids: list[str] | None = None
    doc_types: list[str] | None = None
    departments: list[str] | None = None
    date_from: datetime | None = None   # filters on version upload time
    date_to: datetime | None = None
    temporal: Temporal = "current"
    as_of: datetime | None = None       # required when temporal == "as_of"
    kinds: list[str] | None = None      # text | table | figure
    document_ids: list[str] | None = None  # restrict to specific documents (research contexts, reports)


@dataclass(slots=True)
class Hit:
    chunk_id: str
    document_id: str
    collection_id: str
    title: str
    filename: str
    version: int
    is_current: bool
    page_start: int | None
    page_end: int | None
    section: str | None
    text: str
    parent_text: str | None = None
    kind: str = "text"
    score: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)   # raw per-retriever scores
    ranks: dict[str, int] = field(default_factory=dict)      # 1-based rank per retriever
    compressed_text: str | None = None                       # extractive evidence subset of `text`
    evidence_sentences: list[int] = field(default_factory=list)


@dataclass(slots=True)
class RetrievalResult:
    hits: list[Hit]
    mode: Mode
    timings_ms: dict[str, float]
    candidates: dict[str, int]
    collections_searched: int
