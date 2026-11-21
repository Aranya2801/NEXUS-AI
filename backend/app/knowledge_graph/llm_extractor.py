"""LLM-backed extractor with strict validation. The model's output is treated as untrusted: every entity must occur in the
chunk text, every relation must quote a verbatim sentence from the chunk that mentions both endpoints, predicates are
mapped onto a closed vocabulary, and counts are capped. Anything else is dropped (and counted in `rejected`), so a
hallucinating or prompt-injected model cannot add facts that the document does not literally contain."""
from __future__ import annotations

from app.core.logging import get_logger
from app.knowledge_graph.resolution import canonicalize
from app.knowledge_graph.types import ChunkExtraction, RawEntity, RawRelation
from app.knowledge_graph.vocab import normalize_predicate, normalize_type
from app.llm import prompts
from app.llm.base import LLM, UsageMeter
from app.llm.jsonutil import parse_json_object

log = get_logger("nexus.graph.llm")


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


class LLMExtractor:
    def __init__(self, llm: LLM, meter: UsageMeter | None = None, max_entities: int = 25, max_relations: int = 30) -> None:
        self.llm, self.meter = llm, meter or UsageMeter()
        self.name = f"llm:{llm.name}"
        self.max_entities, self.max_relations = max_entities, max_relations

    async def extract_one(self, text: str) -> ChunkExtraction:
        out = ChunkExtraction()
        try:
            r = await self.llm.complete(prompts.EXTRACT_GRAPH, prompts.extract_user(text), max_tokens=1200)
        except Exception as exc:  # noqa: BLE001
            log.warning("graph extraction failed", extra={"extra_fields": {"error": str(exc)[:200]}})
            raise
        self.meter.add("extract_graph", r)
        obj = parse_json_object(r.text) or {}
        ntext = _norm(text)
        known: dict[str, RawEntity] = {}

        def add_entity(name: str, typ: str = "other") -> bool:
            name = " ".join(name.split())
            if not name or len(name) > 120 or _norm(name) not in ntext:
                return False
            known.setdefault(canonicalize(name), RawEntity(name, normalize_type(typ)))
            return True

        for e in obj.get("entities", []) if isinstance(obj.get("entities"), list) else []:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                if not add_entity(e["name"], str(e.get("type", "other"))):
                    out.rejected += 1
        for rel in obj.get("relations", []) if isinstance(obj.get("relations"), list) else []:
            if not isinstance(rel, dict) or not all(isinstance(rel.get(k), str) for k in ("source", "target", "evidence")):
                out.rejected += 1
                continue
            ev, s, t = " ".join(rel["evidence"].split()), rel["source"], rel["target"]
            if (_norm(ev) not in ntext or _norm(s) not in _norm(ev) or _norm(t) not in _norm(ev)
                    or canonicalize(s) == canonicalize(t) or not (add_entity(s) | (canonicalize(s) in known))
                    or not (add_entity(t) | (canonicalize(t) in known))):
                out.rejected += 1
                continue
            out.relations.append(RawRelation(s, normalize_predicate(str(rel.get("predicate", ""))), t, ev, 0.6))
        out.entities = list(known.values())[: self.max_entities]
        out.relations = out.relations[: self.max_relations]
        return out

    async def extract(self, chunks: list[tuple[str, str]]) -> dict[str, ChunkExtraction]:
        return {cid: await self.extract_one(text) for cid, text in chunks}
