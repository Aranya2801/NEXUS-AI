from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class RawEntity:
    surface: str
    type: str = "concept"


@dataclass(slots=True)
class RawRelation:
    source: str            # surface form
    predicate: str         # normalised vocabulary term
    target: str
    evidence: str          # verbatim sentence from the chunk
    confidence: float = 0.5


@dataclass(slots=True)
class ChunkExtraction:
    entities: list[RawEntity] = field(default_factory=list)
    relations: list[RawRelation] = field(default_factory=list)
    rejected: int = 0      # items dropped by validation (LLM extractor)
