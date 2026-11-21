"""Safe image handling. Uploaded/embedded images are untrusted: they are decoded with Pillow under a pixel budget,
re-encoded to PNG (dropping metadata and any polyglot payload), and OCR'd with tesseract when it is installed."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from app.core.exceptions import IngestionError, ValidationFailed
from app.core.logging import get_logger
from app.document_processing.types import ExtractedImage

log = get_logger("nexus.multimodal")

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp")   # no SVG: it can carry script
_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*", b"BM")


def looks_like_image(data: bytes) -> bool:
    return data.startswith(_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def normalize_image(data: bytes, *, max_pixels: int, max_side: int = 2000) -> tuple[bytes, int, int]:
    """Decode -> verify -> bound -> re-encode as PNG. Raises ValidationFailed for corrupt images or decompression bombs."""
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = max_pixels   # Pillow raises DecompressionBombError above 2x this
    try:
        with Image.open(io.BytesIO(data)) as probe:
            w, h = probe.size
            if w * h > max_pixels:
                raise ValidationFailed(f"Image too large ({w}x{h} px; limit {max_pixels} px)")
            probe.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)                                    # first frame of GIF/TIFF
            im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side))
            out = io.BytesIO()
            im.save(out, "PNG")
            return out.getvalue(), im.size[0], im.size[1]
    except ValidationFailed:
        raise
    except Image.DecompressionBombError as exc:
        raise ValidationFailed("Image rejected (decompression bomb guard)") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ValidationFailed(f"Corrupt or unsupported image: {exc}") from exc


def make_extracted(data: bytes, *, page: int | None, source: str, max_pixels: int, min_pixels: int = 0) -> ExtractedImage | None:
    """Returns None for images below `min_pixels` (icons, bullets) or that cannot be decoded (embedded junk is skipped, not fatal)."""
    try:
        png, w, h = normalize_image(data, max_pixels=max_pixels)
    except ValidationFailed as exc:
        log.warning("embedded image skipped", extra={"extra_fields": {"reason": str(exc)[:120], "source": source}})
        return None
    if w * h < min_pixels:
        return None
    return ExtractedImage(png, w, h, page, source, hashlib.sha256(png).hexdigest()[:16])


@dataclass(slots=True)
class OcrResult:
    text: str
    status: str          # ok | unavailable | failed
    confidence: float | None = None


def tesseract_available() -> bool:
    import shutil

    return shutil.which("tesseract") is not None


def ocr_image(png: bytes, lang: str = "eng", timeout: int = 30) -> OcrResult:
    """Blocking; call via asyncio.to_thread. Never raises: OCR is an enrichment, the document still ingests without it."""
    if not tesseract_available():
        return OcrResult("", "unavailable")
    try:
        import pytesseract
        from PIL import Image

        with Image.open(io.BytesIO(png)) as im:
            data = pytesseract.image_to_data(im, lang=lang, timeout=timeout, output_type=pytesseract.Output.DICT)
        words = [(w, float(c)) for w, c in zip(data["text"], data["conf"]) if w.strip() and float(c) >= 0]
        text = _rebuild_lines(data)
        conf = sum(c for _, c in words) / len(words) / 100 if words else None
        return OcrResult(text.strip(), "ok", round(conf, 3) if conf is not None else None)
    except Exception as exc:  # noqa: BLE001
        log.warning("ocr failed", extra={"extra_fields": {"error": f"{type(exc).__name__}: {exc}"[:200]}})
        return OcrResult("", "failed")


def _rebuild_lines(d: dict) -> str:
    lines: dict[tuple, list[str]] = {}
    for i, w in enumerate(d["text"]):
        if w.strip() and float(d["conf"][i]) >= 0:
            lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(w)
    return "\n".join(" ".join(ws) for ws in lines.values())


# ------------------------------------------------------------------ embedded-image extraction (best effort)
def pdf_images(data: bytes, *, max_images: int, max_pixels: int, min_pixels: int) -> list[ExtractedImage]:
    from pypdf import PdfReader

    out: list[ExtractedImage] = []
    seen: set[str] = set()
    try:
        reader = PdfReader(io.BytesIO(data))
        for pno, page in enumerate(reader.pages, start=1):
            for img in page.images:
                if len(out) >= max_images:
                    return out
                ex = make_extracted(img.data, page=pno, source="pdf", max_pixels=max_pixels, min_pixels=min_pixels)
                if ex and ex.sha not in seen:
                    seen.add(ex.sha)
                    out.append(ex)
    except Exception as exc:  # noqa: BLE001
        log.warning("pdf image extraction failed", extra={"extra_fields": {"error": str(exc)[:200]}})
    return out


def docx_images(data: bytes, *, max_images: int, max_pixels: int, min_pixels: int) -> list[ExtractedImage]:
    import zipfile

    out, seen = [], set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in sorted(n for n in zf.namelist() if n.startswith("word/media/")):
                if len(out) >= max_images:
                    break
                ex = make_extracted(zf.read(name), page=None, source="docx", max_pixels=max_pixels, min_pixels=min_pixels)
                if ex and ex.sha not in seen:
                    seen.add(ex.sha)
                    out.append(ex)
    except Exception as exc:  # noqa: BLE001
        log.warning("docx image extraction failed", extra={"extra_fields": {"error": str(exc)[:200]}})
    return out


def pptx_images(data: bytes, *, max_images: int, max_pixels: int, min_pixels: int) -> list[ExtractedImage]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    out, seen = [], set()
    try:
        prs = Presentation(io.BytesIO(data))
        for sno, slide in enumerate(prs.slides, start=1):
            for shape in slide.shapes:
                if len(out) >= max_images:
                    return out
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    ex = make_extracted(shape.image.blob, page=sno, source="pptx", max_pixels=max_pixels, min_pixels=min_pixels)
                    if ex and ex.sha not in seen:
                        seen.add(ex.sha)
                        out.append(ex)
    except Exception as exc:  # noqa: BLE001
        log.warning("pptx image extraction failed", extra={"extra_fields": {"error": str(exc)[:200]}})
    return out


def parse_image(filename: str, data: bytes, *, max_pixels: int):
    from app.document_processing.types import ParsedDocument
    from pathlib import PurePosixPath

    png, w, h = normalize_image(data, max_pixels=max_pixels)
    if not png:
        raise IngestionError("Empty image")
    stem = PurePosixPath(filename).stem.replace("_", " ").replace("-", " ").strip() or "image"
    img = ExtractedImage(png, w, h, 1, "upload", hashlib.sha256(png).hexdigest()[:16])
    return ParsedDocument(stem, [], PurePosixPath(filename.lower()).suffix.lstrip("."), {"width": w, "height": h}, images=[img])
