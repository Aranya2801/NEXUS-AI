"""Format parsers. Each returns a ParsedDocument whose blocks keep page + section provenance."""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from pathlib import PurePosixPath

from app.core.exceptions import IngestionError, UnsupportedFormat
from app.document_processing.normalize import detect_language, normalize_text
from app.document_processing.types import Block, ParsedDocument

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_NUMBERED_HEADING = re.compile(r"^(\d+(\.\d+)*\.?|[IVX]+\.)\s+[A-Z][^.!?]{2,80}$")


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _title_from(filename: str, fallback_text: str = "") -> str:
    return PurePosixPath(filename).stem.replace("_", " ").replace("-", " ").strip() or fallback_text[:80]


def parse_text(filename: str, data: bytes) -> ParsedDocument:
    """TXT and Markdown. Markdown headings define sections."""
    text = _decode(data)
    is_md = filename.lower().endswith(".md")
    blocks: list[Block] = []
    section: str | None = None
    buf: list[str] = []
    title = None

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            blocks.append(Block(body, None, section))
        buf.clear()

    for line in text.splitlines():
        m = _MD_HEADING.match(line) if is_md else None
        if m:
            flush()
            section = m.group(2)
            title = title or section
        else:
            buf.append(line)
    flush()
    # split into paragraphs so blocks stay small
    out: list[Block] = []
    for b in blocks:
        for para in re.split(r"\n\s*\n", b.text):
            if para.strip():
                out.append(Block(para.strip(), None, b.section))
    return ParsedDocument(title or _title_from(filename, text), out, "md" if is_md else "txt")


def parse_html(filename: str, data: bytes) -> ParsedDocument:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_decode(data), "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "iframe"]):
        tag.decompose()
    title = (soup.title.get_text(strip=True) if soup.title else "") or _title_from(filename)
    blocks: list[Block] = []
    section: str | None = None
    root = soup.body or soup
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "table"]):
        if el.name == "table":
            rows = [" | ".join(c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])) for tr in el.find_all("tr")]
            text = "\n".join(r for r in rows if r.strip(" |"))
            if text:
                blocks.append(Block(text, None, section, "table"))
            continue
        if el.find_parent("table"):
            continue
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name.startswith("h") and len(el.name) == 2:
            section = text
        else:
            blocks.append(Block(text, None, section))
    return ParsedDocument(title, blocks, "html")


def parse_csv(filename: str, data: bytes, rows_per_block: int = 25) -> ParsedDocument:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return ParsedDocument(_title_from(filename), [], "csv")
    header, body = rows[0], rows[1:]
    blocks = []
    for i in range(0, len(body), rows_per_block):
        chunk = body[i : i + rows_per_block]
        lines = ["; ".join(f"{h}: {v}" for h, v in zip(header, r) if v.strip()) for r in chunk]
        blocks.append(Block("\n".join(lines), None, f"rows {i + 1}-{i + len(chunk)}", "table"))
    return ParsedDocument(_title_from(filename), blocks, "csv", {"columns": header, "row_count": len(body)})


def parse_docx(filename: str, data: bytes) -> ParsedDocument:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    blocks: list[Block] = []
    section: str | None = None
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            if style.lower().startswith(("heading", "title")):
                section = text
            else:
                blocks.append(Block(text, None, section))
        elif tag == "tbl":
            t = Table(child, doc)
            rows = [" | ".join(c.text.strip() for c in r.cells) for r in t.rows]
            text = "\n".join(rows).strip()
            if text:
                blocks.append(Block(text, None, section, "table"))
    props = doc.core_properties
    meta = {k: v for k, v in {"author": props.author, "created": str(props.created) if props.created else None}.items() if v}
    return ParsedDocument(props.title or _title_from(filename), blocks, "docx", meta)


def parse_pptx(filename: str, data: bytes) -> ParsedDocument:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    blocks: list[Block] = []
    for i, slide in enumerate(prs.slides, start=1):
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.has_text_frame else None
        section = title or f"Slide {i}"
        for shape in slide.shapes:
            if shape.has_text_frame and shape != slide.shapes.title:
                text = "\n".join(p.text for p in shape.text_frame.paragraphs).strip()
                if text:
                    blocks.append(Block(text, i, section))
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [" | ".join(c.text.strip() for c in r.cells) for r in shape.table.rows]
                blocks.append(Block("\n".join(rows), i, section, "table"))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                blocks.append(Block(f"Speaker notes: {notes}", i, section))
    return ParsedDocument(_title_from(filename), blocks, "pptx", {"slides": len(prs.slides)})


def parse_xlsx(filename: str, data: bytes, rows_per_block: int = 25) -> ParsedDocument:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    blocks: list[Block] = []
    for page, ws in enumerate(wb.worksheets, start=1):
        rows = [[("" if c is None else str(c)) for c in r] for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            continue
        header, body = rows[0], rows[1:] or []
        for i in range(0, max(len(body), 1), rows_per_block):
            chunk = body[i : i + rows_per_block]
            lines = ["; ".join(f"{h}: {v}" for h, v in zip(header, r) if v.strip()) for r in chunk] or [" | ".join(header)]
            blocks.append(Block("\n".join(lines), page, f"Sheet {ws.title}", "table"))
    return ParsedDocument(_title_from(filename), blocks, "xlsx", {"sheets": [w.title for w in wb.worksheets]})


def _strip_running_lines(pages: list[list[str]]) -> list[list[str]]:
    """Drop headers/footers: first/last 2 lines that repeat (digits masked) on >=50% of pages."""
    if len(pages) < 3:
        return pages
    mask = lambda s: re.sub(r"\d+", "#", s.strip().lower())  # noqa: E731
    cand: Counter[str] = Counter()
    for lines in pages:
        edge = lines[:2] + lines[-2:]
        cand.update({mask(x) for x in edge if x.strip()})
    repeated = {k for k, v in cand.items() if v >= max(2, len(pages) * 0.5)}
    return [[ln for ln in lines if mask(ln) not in repeated] for lines in pages]


def parse_pdf(filename: str, data: bytes, ocr_enabled: bool = False) -> ParsedDocument:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise IngestionError("Encrypted PDFs are not supported")
        raw = [(p.extract_text() or "") for p in reader.pages]
    except PdfReadError as exc:
        raise IngestionError(f"Unreadable PDF: {exc}") from exc

    scanned = [i + 1 for i, t in enumerate(raw) if len(t.strip()) < 20]
    if scanned and ocr_enabled:
        raw = _ocr_pages(data, raw, scanned)
        scanned = [i + 1 for i, t in enumerate(raw) if len(t.strip()) < 20]

    pages = _strip_running_lines([normalize_text(t).split("\n") for t in raw])
    blocks: list[Block] = []
    section: str | None = None
    heading_has_body = True      # a heading that never gets a body (e.g. an all-caps scanned line) is kept as text, not dropped
    last_heading: tuple[str, int] | None = None
    for page_no, lines in enumerate(pages, start=1):
        para: list[str] = []

        def flush() -> None:
            nonlocal heading_has_body
            body = " ".join(para).strip()
            if body:
                blocks.append(Block(body, page_no, section))
                heading_has_body = True
            para.clear()

        def close_heading() -> None:
            if last_heading and not heading_has_body:
                blocks.append(Block(last_heading[0], last_heading[1], last_heading[0]))

        for ln in lines:
            s = ln.strip()
            if not s:
                flush()
                continue
            is_heading = len(s) <= 90 and (_NUMBERED_HEADING.match(s) or (s.isupper() and len(s.split()) <= 10 and len(s) > 3))
            if is_heading:
                flush()
                close_heading()
                section, last_heading, heading_has_body = s, (s, page_no), False
            else:
                para.append(s)
        flush()
    close_heading()
    info = reader.metadata
    meta = {"page_count": len(reader.pages), "scanned_pages_without_text": scanned}
    if info and info.title:
        meta["pdf_title"] = str(info.title)
    return ParsedDocument((info.title if info and info.title else None) or _title_from(filename), blocks, "pdf", meta)


def _ocr_pages(data: bytes, raw: list[str], pages: list[int]) -> list[str]:
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except ImportError as exc:  # optional dependency
        raise IngestionError("OCR requested but pytesseract/pdf2image are not installed") from exc
    out = list(raw)
    for p in pages:
        img = convert_from_bytes(data, first_page=p, last_page=p)[0]
        out[p - 1] = pytesseract.image_to_string(img)
    return out


PARSERS = {
    ".txt": parse_text, ".md": parse_text, ".html": parse_html, ".htm": parse_html, ".csv": parse_csv,
    ".docx": parse_docx, ".pptx": parse_pptx, ".xlsx": parse_xlsx,
}


def parse_document(filename: str, data: bytes, *, ocr_enabled: bool = False, multimodal: bool = False,
                   max_images: int = 20, max_pixels: int = 40_000_000, min_pixels: int = 20_000,
                   pdf_tables: bool = True) -> ParsedDocument:
    """`multimodal` additionally extracts embedded images (returned in `doc.images`, enriched later by the ingestion service)
    and PDF tables. Image files are always parsed into a single image; their text comes from OCR / vision."""
    from app.multimodal import images as mm

    ext = PurePosixPath(filename.lower()).suffix
    if ext in mm.IMAGE_EXTENSIONS:
        try:
            return mm.parse_image(filename, data, max_pixels=max_pixels)
        except IngestionError:
            raise
        except Exception as exc:
            if hasattr(exc, "status_code"):
                raise
            raise IngestionError(f"Failed to parse image: {exc}") from exc
    kw = dict(max_images=max_images, max_pixels=max_pixels, min_pixels=min_pixels)
    if ext == ".pdf":
        doc = parse_pdf(filename, data, ocr_enabled)
        if multimodal:
            from app.multimodal.tables import pdf_tables as _tables

            if pdf_tables:
                tables = _tables(data)
                doc.blocks.extend(tables)
                doc.metadata["tables_extracted"] = len(tables)
            doc.images = mm.pdf_images(data, **kw)
    elif ext in PARSERS:
        try:
            doc = PARSERS[ext](filename, data)
        except IngestionError:
            raise
        except Exception as exc:  # corrupt office files etc.
            raise IngestionError(f"Failed to parse {ext} file: {exc}") from exc
        if multimodal and ext == ".docx":
            doc.images = mm.docx_images(data, **kw)
        elif multimodal and ext == ".pptx":
            doc.images = mm.pptx_images(data, **kw)
    else:
        raise UnsupportedFormat(f"Unsupported file type: {ext or 'none'}")
    sample = " ".join(b.text for b in doc.blocks[:40])
    doc.language = detect_language(sample)
    return doc
