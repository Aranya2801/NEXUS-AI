"""Entity canonicalisation, acronym detection and persistent entity resolution (surface form → Entity row)."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Entity, EntityAlias

_LEGAL = re.compile(r"\s+(inc|corp|corporation|ltd|llc|gmbh|co)\.?$")
_LEADING = re.compile(r"^(the|a|an)\s+")
_NONWORD = re.compile(r"[^\w\s]", re.UNICODE)
_SMALL = {"of", "for", "and", "the", "from", "in", "on", "to", "with", "a", "an"}
_ACRONYM = re.compile(r"((?:[A-Z][\w-]*)(?:\s+(?:[A-Za-z][\w-]*))*?)\s*\(\s*([A-Z][A-Za-z0-9-]{1,11})\s*\)")


def canonicalize(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).lower().strip()
    s = s.replace("’s", "").replace("'s", "")
    s = s.replace("-", " ").replace("_", " ")
    s = _NONWORD.sub("", s)
    s = " ".join(s.split())
    s = _LEADING.sub("", s)
    s = _LEGAL.sub("", s)
    words = s.split()
    if words and len(words[-1]) >= 5 and words[-1].endswith("s") and not words[-1].endswith(("ss", "us", "is")):
        words[-1] = words[-1][:-1]  # light plural folding: "transformers" == "transformer"
    return " ".join(words)


def acronym_pairs(text: str) -> list[tuple[str, str]]:
    """'Bidirectional Encoder Representations from Transformers (BERT)' → [(expansion, 'BERT')].
    The acronym letters must be a subsequence of the expansion's word initials."""
    out = []
    for m in _ACRONYM.finditer(text):
        expansion, acr = m.group(1).strip(), m.group(2)
        words = expansion.split()
        # trim leading words so the expansion is the shortest suffix whose initials cover the acronym
        for start in range(len(words)):
            cand = words[start:]
            initials = [w[0].upper() for w in cand if w.lower() not in _SMALL]
            it = iter(initials)
            core = acr[:-1] if acr.endswith("s") and acr[:-1].isupper() else acr  # LLMs -> LLM
            if len(cand) >= 2 and all(ch in it for ch in core.upper()):
                out.append((" ".join(cand), acr))
                break
    return out


class EntityResolver:
    """Resolves surface forms to persistent entities within one organisation.

    Order: (1) in-batch cache, (2) exact canonical/alias match in the DB, (3) conservative fuzzy match
    (same type, ≥6 chars, difflib ratio ≥ 0.94), (4) create. Acronym definitions found in the same document
    map the short form onto the expansion so both resolve to one entity."""

    FUZZY = 0.94

    def __init__(self, db: AsyncSession, org_id: str, local_aliases: dict[str, str] | None = None) -> None:
        self.db, self.org_id = db, org_id
        self.local = local_aliases or {}   # canonical acronym → canonical expansion
        self._cache: dict[str, Entity] = {}

    async def resolve_many(self, surfaces: dict[str, str]) -> dict[str, Entity]:
        """surfaces: surface → type. Returns surface → Entity."""
        by_canon: dict[str, tuple[str, str]] = {}
        for surf, typ in surfaces.items():
            c = canonicalize(surf)
            c = self.local.get(c, c)
            if c:
                by_canon.setdefault(c, (surf, typ))
        need = [c for c in by_canon if c not in self._cache]
        if need:
            rows = (await self.db.execute(
                select(EntityAlias.alias_canonical, Entity).join(Entity, Entity.id == EntityAlias.entity_id)
                .where(EntityAlias.org_id == self.org_id, EntityAlias.alias_canonical.in_(need)))).all()
            for alias, ent in rows:
                self._cache.setdefault(alias, ent)
        for c in [c for c in by_canon if c not in self._cache]:
            surf, typ = by_canon[c]
            ent = await self._fuzzy(c, typ) or await self._create(c, surf, typ)
            self._cache[c] = ent
        out: dict[str, Entity] = {}
        for surf in surfaces:
            c = canonicalize(surf)
            c = self.local.get(c, c)
            if c in self._cache:
                out[surf] = self._cache[c]
                await self._learn_alias(self._cache[c], canonicalize(surf))
        return out

    async def _fuzzy(self, canon: str, typ: str) -> Entity | None:
        if len(canon) < 6:
            return None
        rows = (await self.db.execute(select(Entity).where(
            Entity.org_id == self.org_id, Entity.type == typ, Entity.canonical_name.like(canon[:4] + "%")).limit(60))).scalars().all()
        best = max(((SequenceMatcher(None, canon, e.canonical_name).ratio(), e) for e in rows), key=lambda t: t[0], default=(0, None))
        return best[1] if best[1] is not None and best[0] >= self.FUZZY else None

    async def _create(self, canon: str, surface: str, typ: str) -> Entity:
        ent = Entity(org_id=self.org_id, canonical_name=canon, display_name=surface.strip()[:300], type=typ, aliases=[])
        self.db.add(ent)
        await self.db.flush()
        self.db.add(EntityAlias(org_id=self.org_id, entity_id=ent.id, alias_canonical=canon))
        return ent

    async def _learn_alias(self, ent: Entity, alias_canon: str) -> None:
        if not alias_canon or alias_canon == ent.canonical_name or alias_canon in {a for a in (ent.aliases or [])}:
            return
        exists = (await self.db.execute(select(EntityAlias.id).where(
            EntityAlias.entity_id == ent.id, EntityAlias.alias_canonical == alias_canon))).first()
        if not exists:
            self.db.add(EntityAlias(org_id=self.org_id, entity_id=ent.id, alias_canonical=alias_canon))
            ent.aliases = [*(ent.aliases or []), alias_canon]
