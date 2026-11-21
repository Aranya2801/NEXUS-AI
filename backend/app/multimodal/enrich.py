"""Ingestion enrichment: OCR + vision caption for every extracted image. Produces `ocr`/`figure` blocks (appended to the parsed
document before chunking) and a per-figure record stored in `Document.meta["figures"]`. Failures degrade, never abort."""
from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.core.logging import get_logger
from app.document_processing.types import Block, ParsedDocument
from app.multimodal.images import ocr_image

log = get_logger("nexus.multimodal")
MIN_OCR_CHARS = 3


async def enrich_images(parsed: ParsedDocument, s: Settings, vision, storage, key_prefix: str) -> list[dict]:
    figures: list[dict] = []
    for i, img in enumerate(parsed.images[: s.max_images_per_document], start=1):
        key = f"{key_prefix}/{i}.png"
        await storage.put(key, img.data)
        rec = {"index": i, "page": img.page, "source": img.source, "width": img.width, "height": img.height, "storage_key": key,
               "sha": img.sha, "ocr": None, "caption": None}
        section = f"Figure {i}" + (f" (page {img.page})" if img.page else "")
        if s.image_ocr_enabled:
            res = await asyncio.to_thread(ocr_image, img.data)
            rec["ocr"] = {"status": res.status, "chars": len(res.text), "confidence": res.confidence}
            if res.status == "ok" and len(res.text) >= MIN_OCR_CHARS:
                parsed.blocks.append(Block(res.text, img.page, section, "ocr"))
        if vision is not None:
            try:
                cap = await vision.describe(img.data)
                rec["caption"] = {"status": "ok", "model": vision.name, "chars": len(cap)}
                if cap:
                    parsed.blocks.append(Block(f"{section}: {cap}", img.page, section, "figure"))
            except Exception as exc:  # noqa: BLE001 - captioning is an enrichment
                rec["caption"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:200]}
                log.warning("vision caption failed", extra={"extra_fields": {"figure": i, "error": rec["caption"]["error"]}})
        figures.append(rec)
    return figures
