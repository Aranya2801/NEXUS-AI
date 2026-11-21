from __future__ import annotations

ENTITY_TYPES = ("organization", "person", "model", "concept", "dataset", "metric", "event", "document", "other")
PREDICATES = ("improves", "extends", "outperforms", "based_on", "uses", "proposes", "evaluated_on",
              "limited_by", "compared_with", "part_of", "related_to")

_SYNONYMS = {
    "improve": "improves", "enhance": "improves", "boost": "improves", "extend": "extends", "generalize": "extends",
    "builds_on": "extends", "outperform": "outperforms", "surpass": "outperforms", "beats": "outperforms",
    "based_on": "based_on", "derived_from": "based_on", "adapts": "based_on", "use": "uses", "employs": "uses",
    "leverages": "uses", "relies_on": "uses", "propose": "proposes", "introduce": "proposes", "introduces": "proposes",
    "present": "proposes", "presents": "proposes", "evaluated_on": "evaluated_on", "tested_on": "evaluated_on",
    "trained_on": "evaluated_on", "limited_by": "limited_by", "suffers_from": "limited_by", "limitation": "limited_by",
    "compared_with": "compared_with", "compares_to": "compared_with", "versus": "compared_with", "part_of": "part_of",
    "component_of": "part_of", "related_to": "related_to", "related": "related_to",
}


def normalize_predicate(raw: str) -> str:
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if key in PREDICATES:
        return key
    return _SYNONYMS.get(key, _SYNONYMS.get(key.rstrip("s"), "related_to"))


def normalize_type(raw: str) -> str:
    t = (raw or "").strip().lower()
    return t if t in ENTITY_TYPES else "other"
