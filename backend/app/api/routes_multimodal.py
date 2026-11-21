"""Multimodal endpoints: figure retrieval for source viewing, and image-grounded search."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal
from app.api.routes_kb import _visible_document
from app.api.routes_search import HitOut
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.exceptions import NotFound, ValidationFailed
from app.multimodal.images import looks_like_image, normalize_image, ocr_image
from app.retrieval.types import SearchFilters

router = APIRouter(tags=["multimodal"])
MAX_QUERY_CHARS = 1800


class FigureOut(BaseModel):
    index: int
    page: int | None
    source: str
    width: int
    height: int
    ocr: dict | None = None
    caption: dict | None = None
    url: str


def _figures(doc) -> list[dict]:
    return (doc.meta or {}).get("figures", [])


@router.get("/documents/{document_id}/figures", response_model=list[FigureOut])
async def list_figures(document_id: str, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    doc = await _visible_document(db, p, document_id)
    return [FigureOut(**{k: f.get(k) for k in ("index", "page", "source", "width", "height", "ocr", "caption")},
                      url=f"/documents/{doc.id}/figures/{f['index']}") for f in _figures(doc)]


@router.get("/documents/{document_id}/figures/{index}")
async def get_figure(document_id: str, index: int, request: Request, p: Principal = Depends(get_principal),
                     db: AsyncSession = Depends(get_db)):
    doc = await _visible_document(db, p, document_id)   # same access rule as the document itself; hidden -> 404
    rec = next((f for f in _figures(doc) if f["index"] == index), None)
    if rec is None:
        raise NotFound("Figure not found")
    data = await request.app.state.ingestion.storage.get(rec["storage_key"])
    return Response(data, media_type="image/png", headers={"Content-Security-Policy": "default-src 'none'", "Cache-Control": "private, max-age=300"})


class MultimodalSearchOut(BaseModel):
    derived_query: str
    ocr: dict
    caption: str | None
    hits: list[HitOut]
    timings_ms: dict[str, float]


@router.post("/search/multimodal", response_model=MultimodalSearchOut)
async def search_multimodal(request: Request, image: UploadFile = File(...), query: str = Form(""), top_k: int = Form(8, ge=1, le=50),
                            collection_ids: str | None = Form(None, description="comma-separated"),
                            p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    """Search by image (a screenshot, a chart, a scanned page), optionally with a text question. The image is OCR'd and
    captioned; the derived text is the retrieval query. The image itself is never stored."""
    s = request.app.state.settings
    data = await image.read(s.max_upload_bytes + 1)
    if not data or len(data) > s.max_upload_bytes or not looks_like_image(data):
        raise ValidationFailed("A valid image (png, jpg, webp, gif, tiff, bmp) up to the upload limit is required")
    png, _, _ = await asyncio.to_thread(normalize_image, data, max_pixels=s.max_image_pixels)
    ocr = await asyncio.to_thread(ocr_image, png) if s.image_ocr_enabled else None
    vision, caption = request.app.state.vision, None
    if vision is not None:
        try:
            caption = await vision.describe(png)
        except Exception:  # noqa: BLE001 - OCR alone can still answer
            caption = None
    parts = [query.strip(), ocr.text if ocr else "", caption or ""]
    derived = " ".join(" ".join(x.split()) for x in parts if x.strip())[:MAX_QUERY_CHARS]
    if not derived:
        raise ValidationFailed("No text could be derived from the image (OCR unavailable or empty and no vision model configured); add a text query")
    ids = [c for c in (collection_ids or "").split(",") if c] or None
    res = await request.app.state.retriever.search(db, p, derived, mode="hybrid", top_k=top_k, filters=SearchFilters(collection_ids=ids))
    await audit(db, "search.multimodal", org_id=p.org_id, user_id=p.user_id, image_sha=hashlib.sha256(png).hexdigest()[:16],
                query_sha=hashlib.sha256(derived.encode()).hexdigest()[:16], hits=len(res.hits))
    return MultimodalSearchOut(derived_query=derived, ocr={"status": ocr.status if ocr else "disabled", "chars": len(ocr.text) if ocr else 0,
                                                           "confidence": ocr.confidence if ocr else None},
                               caption=caption, hits=[HitOut(**asdict(h)) for h in res.hits], timings_ms=res.timings_ms)
