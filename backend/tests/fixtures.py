"""Programmatic builders for test documents (no binary fixtures committed)."""
from __future__ import annotations

import io


def make_pdf(pages: list[list[str]]) -> bytes:
    """Minimal valid multi-page PDF; each page is a list of text lines (Helvetica)."""
    objs: list[bytes] = []
    n = len(pages)
    page_ids = [3 + 2 * i for i in range(n)]
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, lines in enumerate(pages):
        content_id = page_ids[i] + 1
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        ops = ["BT", "/F1 11 Tf", "14 TL", "50 740 Td"]
        for ln in lines:
            esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({esc}) '")
        ops.append("ET")
        stream = "\n".join(ops).encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


def make_docx() -> bytes:
    from docx import Document

    d = Document()
    d.add_heading("Transformer Survey", level=1)
    d.add_paragraph("Attention mechanisms were introduced to model long-range dependencies.")
    d.add_heading("Results", level=2)
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Model", "BLEU"
    t.cell(1, 0).text, t.cell(1, 1).text = "Base", "27.3"
    d.add_paragraph("The base model reached 27.3 BLEU on the benchmark.")
    b = io.BytesIO(); d.save(b); return b.getvalue()


def make_pptx() -> bytes:
    from pptx import Presentation

    p = Presentation()
    s = p.slides.add_slide(p.slide_layouts[1])
    s.shapes.title.text = "Motivation"
    s.placeholders[1].text = "Retrieval improves factual accuracy."
    s2 = p.slides.add_slide(p.slide_layouts[1])
    s2.shapes.title.text = "Limitations"
    s2.placeholders[1].text = "Latency grows with reranking depth."
    b = io.BytesIO(); p.save(b); return b.getvalue()


def make_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook(); ws = wb.active; ws.title = "Metrics"
    ws.append(["model", "precision", "recall"]); ws.append(["bm25", 0.61, 0.52]); ws.append(["hybrid", 0.72, 0.66])
    b = io.BytesIO(); wb.save(b); return b.getvalue()
