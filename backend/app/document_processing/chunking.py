"""Section-aware parent/child chunker with overlap and optional lexical-semantic boundaries.

* Chunks never cross a section boundary.
* Children are the retrieval units; parents are exact source slices that give the LLM wider context.
* Every chunk carries char offsets into `ChunkSet.full_text`, page range and section name.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from app.document_processing.normalize import count_tokens, normalize_text
from app.document_processing.types import Block, ChunkDraft, ChunkSet, ParsedDocument

_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_LINE = re.compile(r"\n")
_WORD = re.compile(r"\S+")
_TOKEN = re.compile(r"\w+")


@dataclass(slots=True)
class _Unit:
    text: str
    start: int
    end: int
    tokens: int
    page: int | None
    section: str | None
    sep: str
    kind: str = "text"


def _spans(text: str, pattern: re.Pattern[str]):
    start = 0
    for m in pattern.finditer(text):
        yield start, m.start()
        start = m.end()
    yield start, len(text)


def _chunk_kind(block_kind: str) -> str:
    return "figure" if block_kind in ("figure", "ocr") else "table" if block_kind == "table" else "text"


def _units_for_block(block: Block, base: int, target: int) -> list[_Unit]:
    pattern = _LINE if block.kind == "table" else _SENT_BOUNDARY
    sep = "\n" if block.kind == "table" else " "
    units: list[_Unit] = []
    for s, e in _spans(block.text, pattern):
        piece = block.text[s:e].strip()
        if not piece:
            continue
        lead = len(block.text[s:e]) - len(block.text[s:e].lstrip())
        s += lead
        tokens = count_tokens(piece)
        if tokens <= target:
            units.append(_Unit(piece, base + s, base + s + len(piece), tokens, block.page, block.section, sep, _chunk_kind(block.kind)))
            continue
        words = list(_WORD.finditer(piece))  # hard-split an oversized sentence/row on word windows
        for i in range(0, len(words), target):
            win = words[i : i + target]
            ws, we = win[0].start(), win[-1].end()
            txt = piece[ws:we]
            units.append(_Unit(txt, base + s + ws, base + s + we, count_tokens(txt), block.page, block.section, " ", _chunk_kind(block.kind)))
    return units


def _bow_cosine(a: str, b: str) -> float:
    ca, cb = Counter(_TOKEN.findall(a.lower())), Counter(_TOKEN.findall(b.lower()))
    dot = sum(ca[t] * cb[t] for t in ca.keys() & cb.keys())
    na, nb = math.sqrt(sum(v * v for v in ca.values())), math.sqrt(sum(v * v for v in cb.values()))
    return dot / (na * nb) if na and nb else 0.0


def build_full_text(blocks: list[Block]) -> tuple[str, list[int]]:
    offsets, parts, pos = [], [], 0
    for b in blocks:
        offsets.append(pos)
        parts.append(b.text)
        pos += len(b.text) + 2
    return "\n\n".join(parts), offsets


def chunk_document(
    doc: ParsedDocument,
    *,
    target_tokens: int = 220,
    overlap_tokens: int = 40,
    parent_tokens: int = 800,
    semantic: bool = False,
    semantic_threshold: float = 0.08,
    min_semantic_tokens: int = 60,
) -> ChunkSet:
    if overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens must be smaller than target_tokens")
    blocks = [Block(normalize_text(b.text), b.page, b.section, b.kind) for b in doc.blocks if b.text.strip()]
    blocks = [b for b in blocks if b.text]
    full_text, offsets = build_full_text(blocks)

    units: list[_Unit] = []
    for blk, off in zip(blocks, offsets):
        units.extend(_units_for_block(blk, off, target_tokens))

    children: list[ChunkDraft] = []
    parents: list[ChunkDraft] = []
    child_parent: list[int] = []

    def emit_child(cur: list[_Unit]) -> None:
        text = cur[0].text
        for prev, u in zip(cur, cur[1:]):
            text += u.sep + u.text if u.sep == "\n" else " " + u.text
        pages = [u.page for u in cur if u.page is not None]
        children.append(ChunkDraft(
            ordinal=len(children), level="child", text=text, char_start=cur[0].start, char_end=cur[-1].end,
            token_count=sum(u.tokens for u in cur), page_start=min(pages) if pages else None,
            page_end=max(pages) if pages else None, section=cur[0].section, kind=cur[0].kind,
        ))

    def flush_section(sec_units: list[_Unit]) -> None:
        first_child = len(children)
        cur: list[_Unit] = []
        cur_tokens = fresh = 0
        for u in sec_units:
            sem_break = (
                semantic and cur and cur_tokens >= min_semantic_tokens
                and _bow_cosine(" ".join(x.text for x in cur[-3:]), u.text) < semantic_threshold
            )
            if cur and (cur_tokens + u.tokens > target_tokens or sem_break):
                emit_child(cur)
                keep: list[_Unit] = []
                acc = 0
                for x in reversed(cur):  # trailing units become the overlap for the next chunk
                    if acc + x.tokens > overlap_tokens or len(keep) == len(cur) - 1:
                        break
                    keep.insert(0, x)
                    acc += x.tokens
                cur, cur_tokens, fresh = keep, acc, 0
            cur.append(u)
            cur_tokens += u.tokens
            fresh += 1
        if cur and fresh:
            emit_child(cur)
        # parents: consecutive children of this section, exact source slices
        group: list[ChunkDraft] = []
        acc_tokens = 0
        for ch in children[first_child:]:
            if group and acc_tokens + ch.token_count > parent_tokens:
                _emit_parent(group)
                group, acc_tokens = [], 0
            group.append(ch)
            acc_tokens += ch.token_count - (overlap_tokens if len(group) > 1 else 0)
        if group:
            _emit_parent(group)

    def _emit_parent(group: list[ChunkDraft]) -> None:
        pages = [p for g in group for p in (g.page_start, g.page_end) if p is not None]
        pidx = len(parents)
        text = full_text[group[0].char_start : group[-1].char_end]
        parents.append(ChunkDraft(
            ordinal=pidx, level="parent", text=text, char_start=group[0].char_start,
            char_end=group[-1].char_end, token_count=count_tokens(text),
            page_start=min(pages) if pages else None, page_end=max(pages) if pages else None,
            section=group[0].section, kind=group[0].kind,
        ))
        for g in group:
            g.parent_ordinal = pidx

    sec_units: list[_Unit] = []
    for u in units:
        if sec_units and (u.section != sec_units[-1].section or u.kind != sec_units[-1].kind):   # figures never merge with prose
            flush_section(sec_units)
            sec_units = []
        sec_units.append(u)
    if sec_units:
        flush_section(sec_units)
    return ChunkSet(full_text=full_text, parents=parents, children=children)
