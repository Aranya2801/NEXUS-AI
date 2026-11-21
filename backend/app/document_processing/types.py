from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Block:
    """A contiguous piece of parsed content with its provenance."""

    text: str
    page: int | None = None          # page / slide number (1-based) when the format has one
    section: str | None = None
    kind: str = "text"               # text | table | heading | figure (vision caption) | ocr (text read from an image)


@dataclass(slots=True)
class ExtractedImage:
    """A normalised PNG found in (or being) a document, awaiting OCR / captioning."""

    data: bytes
    width: int
    height: int
    page: int | None
    source: str                      # upload | pdf | docx | pptx
    sha: str


@dataclass(slots=True)
class ParsedDocument:
    title: str
    blocks: list[Block]
    doc_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    language: str | None = None
    images: list[ExtractedImage] = field(default_factory=list)


@dataclass(slots=True)
class ChunkDraft:
    ordinal: int
    level: str                       # parent | child
    text: str
    char_start: int
    char_end: int
    token_count: int
    page_start: int | None
    page_end: int | None
    section: str | None
    parent_ordinal: int | None = None
    kind: str = "text"               # text | table | figure  (figure covers captions and OCR text)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChunkSet:
    full_text: str
    parents: list[ChunkDraft]
    children: list[ChunkDraft]
