"""Phase 11: images, OCR, tables, vision adapters, kind filter, access control on figures."""
from __future__ import annotations

import io
import json

import httpx
import pytest
import pytest_asyncio
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from app.core.config import Settings
from app.core.exceptions import ValidationFailed
from app.database.models import Chunk
from app.main import create_app
from app.multimodal import images as mm
from app.multimodal.vision import AnthropicVision, MockVision, OpenAIVision, build_vision
from tests.test_api_ingestion import register

needs_tesseract = pytest.mark.skipif(not mm.tesseract_available(), reason="tesseract not installed")
PHRASE = "QUARTERLY REVENUE ZEBRAFISH 2047"


def text_image(text: str = PHRASE, fmt: str = "PNG", size=(1100, 160)) -> bytes:
    im = Image.new("RGB", size, "white")
    ImageDraw.Draw(im).text((20, 40), text, fill="black", font=ImageFont.load_default(size=56))
    b = io.BytesIO()
    im.save(b, fmt)
    return b.getvalue()


@pytest.fixture
def cfg(tmp_path) -> Settings:
    return Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", storage_dir=str(tmp_path / "s"), log_json=False,
                    embedding_dim=64, ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4, vision_provider="mock",
                    ingestion_max_attempts=1)


@pytest_asyncio.fixture
async def app(cfg):
    a = create_app(cfg)
    async with a.router.lifespan_context(a):
        yield a


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def setup(client, org="Org A", email="a@a.io"):
    h = await register(client, org, email)
    cid = (await client.post("/collections", json={"name": "kb"}, headers=h)).json()["id"]
    return h, cid


async def up(client, app, h, cid, name, data):
    r = await client.post(f"/collections/{cid}/documents", files={"file": (name, data)}, headers=h)
    if r.status_code == 202:
        await app.state.runner.drain()
    return r


# ---------------------------------------------------------------- validation / safety
def test_image_magic_and_bomb_guard():
    assert mm.looks_like_image(text_image()) and not mm.looks_like_image(b"<svg onload=alert(1)>")
    png, w, h = mm.normalize_image(text_image(), max_pixels=10_000_000)
    assert png.startswith(b"\x89PNG") and (w, h) == (1100, 160)
    with pytest.raises(ValidationFailed):
        mm.normalize_image(text_image(size=(900, 900)), max_pixels=10_000)          # over the pixel budget
    with pytest.raises(ValidationFailed):
        mm.normalize_image(b"\x89PNG\r\n\x1a\n" + b"junk" * 50, max_pixels=10_000_000)  # right magic, corrupt body


def test_normalize_strips_metadata_and_downsizes():
    im = Image.new("RGB", (3000, 100), "white")
    b = io.BytesIO()
    im.save(b, "JPEG")
    png, w, _ = mm.normalize_image(b.getvalue(), max_pixels=10_000_000, max_side=1000)
    assert w == 1000 and png.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_upload_rejects_svg_and_spoofed_image(client, app):
    h, cid = await setup(client)
    assert (await up(client, app, h, cid, "x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")).status_code == 422
    assert (await up(client, app, h, cid, "fake.png", b"not an image at all")).status_code == 422


# ---------------------------------------------------------------- OCR (real tesseract)
@needs_tesseract
def test_real_ocr_reads_generated_image():
    res = mm.ocr_image(text_image())
    assert res.status == "ok" and "ZEBRAFISH" in res.text.upper() and (res.confidence or 0) > 0.5


@needs_tesseract
@pytest.mark.asyncio
async def test_image_upload_is_searchable_by_ocr_text_and_figure_served(client, app):
    h, cid = await setup(client)
    r = await up(client, app, h, cid, "chart.png", text_image())
    assert r.status_code == 202, r.text
    doc_id = r.json()["document"]["id"]
    hits = (await client.post("/search", json={"query": "zebrafish revenue", "kinds": ["figure"]}, headers=h)).json()["hits"]
    assert hits and hits[0]["document_id"] == doc_id and hits[0]["kind"] == "figure"
    assert (await client.post("/search", json={"query": "zebrafish revenue", "kinds": ["table"]}, headers=h)).json()["hits"] == []
    figs = (await client.get(f"/documents/{doc_id}/figures", headers=h)).json()
    assert len(figs) == 1 and figs[0]["ocr"]["status"] == "ok" and figs[0]["caption"]["model"] == "mock-vision"
    img = await client.get(figs[0]["url"], headers=h)
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content.startswith(b"\x89PNG")
    assert "default-src 'none'" in img.headers["content-security-policy"]


@needs_tesseract
@pytest.mark.asyncio
async def test_figures_are_tenant_isolated(client, app):
    h1, c1 = await setup(client, "Org A", "a@a.io")
    doc = (await up(client, app, h1, c1, "chart.png", text_image())).json()["document"]["id"]
    h2 = await register(client, "Org B", "b@b.io")
    assert (await client.get(f"/documents/{doc}/figures", headers=h2)).status_code == 404
    assert (await client.get(f"/documents/{doc}/figures/1", headers=h2)).status_code == 404


@needs_tesseract
def test_scanned_pdf_is_ocrd():
    from app.document_processing.parsers import parse_document

    b = io.BytesIO()
    Image.open(io.BytesIO(text_image())).convert("RGB").save(b, "PDF")     # image-only PDF: no text layer
    doc = parse_document("scan.pdf", b.getvalue(), ocr_enabled=True)
    assert "ZEBRAFISH" in " ".join(x.text for x in doc.blocks).upper()
    assert parse_document("scan.pdf", b.getvalue(), ocr_enabled=False).blocks == []


@needs_tesseract
@pytest.mark.asyncio
async def test_multimodal_search_by_image(client, app):
    h, cid = await setup(client)
    await up(client, app, h, cid, "notes.txt", (f"The {PHRASE.lower()} figure was reviewed by finance. " * 6).encode())
    other = await up(client, app, h, cid, "other.txt", b"Completely unrelated gardening advice about tomatoes and soil. " * 8)
    assert other.status_code == 202
    r = await client.post("/search/multimodal", files={"image": ("q.png", text_image())}, data={"top_k": "3"}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "ZEBRAFISH" in d["derived_query"].upper() and d["ocr"]["status"] == "ok" and d["hits"][0]["filename"] == "notes.txt"
    bad = await client.post("/search/multimodal", files={"image": ("q.png", b"nope")}, headers=h)
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_multimodal_search_without_any_text_is_rejected(client, app, monkeypatch):
    h, _ = await setup(client)
    monkeypatch.setattr(app.state, "vision", None)
    monkeypatch.setattr(app.state.settings, "image_ocr_enabled", False)
    r = await client.post("/search/multimodal", files={"image": ("q.png", text_image())}, headers=h)
    assert r.status_code == 422 and "No text could be derived" in r.text


# ---------------------------------------------------------------- tables & embedded images
def _pdf_with_table() -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    from reportlab.lib import colors

    buf = io.BytesIO()
    t = Table([["Region", "Revenue", "Growth"], ["Nordics", "4200", "12%"], ["Iberia", "3100", "7%"]])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    SimpleDocTemplate(buf, pagesize=letter).build([t])
    return buf.getvalue()


def test_pdf_table_extraction():
    pytest.importorskip("reportlab")
    from app.document_processing.parsers import parse_document

    doc = parse_document("t.pdf", _pdf_with_table(), multimodal=True)
    tables = [b for b in doc.blocks if b.kind == "table"]
    assert tables and "Nordics | 4200 | 12%" in tables[0].text and doc.metadata["tables_extracted"] == 1


def test_docx_table_and_embedded_image(tmp_path):
    from docx import Document

    from app.document_processing.parsers import parse_document

    d = Document()
    d.add_paragraph("Intro paragraph about the figure below.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Item", "Cost", "Widget", "99"
    png = tmp_path / "f.png"
    png.write_bytes(text_image())
    d.add_picture(str(png))
    b = io.BytesIO()
    d.save(b)
    doc = parse_document("d.docx", b.getvalue(), multimodal=True)
    assert any(x.kind == "table" and "Widget | 99" in x.text for x in doc.blocks)
    assert len(doc.images) == 1 and doc.images[0].source == "docx"
    assert parse_document("d.docx", b.getvalue(), multimodal=False).images == []


def test_pptx_picture_extracted_and_small_icons_skipped(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches

    from app.document_processing.parsers import parse_document

    big, small = tmp_path / "big.png", tmp_path / "small.png"
    big.write_bytes(text_image())
    Image.new("RGB", (16, 16), "red").save(small)
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])
    s.shapes.add_picture(str(big), Inches(1), Inches(1))
    s.shapes.add_picture(str(small), Inches(1), Inches(3))
    b = io.BytesIO()
    prs.save(b)
    doc = parse_document("p.pptx", b.getvalue(), multimodal=True, min_pixels=20_000)
    assert [i.page for i in doc.images] == [1] and doc.images[0].width == 1100


# ---------------------------------------------------------------- chunk kinds & vision adapters
def test_chunker_keeps_figures_and_tables_separate_from_prose():
    from app.document_processing.chunking import chunk_document
    from app.document_processing.types import Block, ParsedDocument

    doc = ParsedDocument("t", [Block("Plain prose about the system.", 1, "S"), Block("a | b\nc | d", 1, "S", "table"),
                               Block("Figure 1: a bar chart of sales.", 1, "S", "figure")], "pdf")
    cs = chunk_document(doc)
    assert [c.kind for c in cs.children] == ["text", "table", "figure"]


@pytest.mark.asyncio
async def test_openai_vision_request_shape_and_retry():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": " A bar chart. "}}]})

    v = OpenAIVision(model="m", api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), backoff=0)
    assert await v.describe(b"\x89PNG") == "A bar chart."
    part = calls[-1]["messages"][0]["content"][1]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,") and len(calls) == 2


@pytest.mark.asyncio
async def test_anthropic_vision_request_shape():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["h"], seen["b"] = dict(req.headers), json.loads(req.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "A diagram."}]})

    v = AnthropicVision(model="m", api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await v.describe(b"\x89PNG") == "A diagram."
    src = seen["b"]["messages"][0]["content"][0]
    assert seen["h"]["x-api-key"] == "k" and src["type"] == "image" and src["source"]["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_mock_vision_and_factory(cfg):
    assert "mock caption" in await MockVision().describe(text_image())
    assert isinstance(build_vision(cfg), MockVision)
    assert build_vision(cfg.model_copy(update={"vision_provider": "none"})) is None


@pytest.mark.asyncio
async def test_vision_failure_degrades_without_failing_ingestion(client, app):
    class Boom:
        name = "boom"

        async def describe(self, *a, **k):
            raise RuntimeError("provider down")

    app.state.ingestion.vision = Boom()
    h, cid = await setup(client)
    r = await up(client, app, h, cid, "c.png", text_image())
    doc_id = r.json()["document"]["id"]
    st = (await client.get(f"/documents/{doc_id}", headers=h)).json()["status"]
    if mm.tesseract_available():
        assert st == "ready"
        figs = (await client.get(f"/documents/{doc_id}/figures", headers=h)).json()
        assert figs[0]["caption"]["status"] == "failed"
    else:
        assert st == "failed"          # nothing to index: no OCR and no caption


@pytest.mark.asyncio
async def test_chunk_kind_persisted(client, app):
    h, cid = await setup(client)
    await up(client, app, h, cid, "m.md", b"# Sec\n\nSome ordinary prose goes here for the test.\n")
    async with app.state.session_factory() as db:
        kinds = {c.kind for c in (await db.execute(select(Chunk))).scalars()}
    assert kinds == {"text"}
