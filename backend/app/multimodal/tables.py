"""Table extraction from PDFs (pdfplumber). Rows become `table` blocks, same representation as DOCX/HTML tables
(` | `-joined cells, one row per line) so they chunk row-wise. The same text also appears in the page's flowing text; that
duplication is accepted (retrieval fusion dedupes by chunk, not by content) and documented in docs/multimodal.md."""
from __future__ import annotations

import io

from app.core.logging import get_logger
from app.document_processing.types import Block

log = get_logger("nexus.multimodal")


def pdf_tables(data: bytes, *, max_tables: int = 50) -> list[Block]:
    import pdfplumber

    blocks: list[Block] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                for tno, table in enumerate(page.extract_tables(), start=1):
                    rows = [" | ".join((c or "").replace("\n", " ").strip() for c in row) for row in table if row]
                    text = "\n".join(r for r in rows if r.strip(" |"))
                    if text:
                        blocks.append(Block(text, pno, f"Table {tno} (page {pno})", "table"))
                    if len(blocks) >= max_tables:
                        return blocks
    except Exception as exc:  # noqa: BLE001 - tables are an enrichment
        log.warning("pdf table extraction failed", extra={"extra_fields": {"error": f"{type(exc).__name__}: {exc}"[:200]}})
    return blocks
