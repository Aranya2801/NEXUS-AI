"""Exporters: Markdown, DOCX (python-docx) and PDF (reportlab). All three render the same structured report; citations are
shown as [n] markers with a Sources list (document, page, section, version, verbatim quote). PDF uses DejaVu Sans when found
(or NEXUS_PDF_FONT_PATH) for Unicode; otherwise Helvetica, in which case characters outside Latin-1 are replaced by '?'."""
from __future__ import annotations

import io
import re
from pathlib import Path
from xml.sax.saxutils import escape

DISCLAIMER = ("Statements are either claims verified against the cited passages or verbatim source sentences. Sections marked heuristic "
              "list candidates only. Verify important findings in the original documents.")
_FONT_CANDIDATES = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                    "/usr/local/share/fonts/DejaVuSans.ttf", "/Library/Fonts/DejaVuSans.ttf"]


def marks(ids: list[int]) -> str:
    return "".join(f"[{i}]" for i in ids)


def page_label(c: dict) -> str:
    if not c.get("page_start"):
        return ""
    return f"p.{c['page_start']}" if c.get("page_end") in (None, c["page_start"]) else f"pp.{c['page_start']}-{c['page_end']}"


def source_line(c: dict) -> str:
    if c.get("hidden"):
        return f"[{c['id']}] [restricted source]"
    extra = ", ".join(x for x in (page_label(c), f"section: {c['section']}" if c.get("section") else "", f"v{c['version']}" + ("" if c.get("is_current", True) else " (superseded)")) if x)
    return f"[{c['id']}] {c['title']} ({c['filename']}){' - ' + extra if extra else ''}"


def safe_filename(title: str, ext: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._") or "report"
    return f"{base[:80]}.{ext}"


def _items(block: dict) -> list[dict]:
    return block["items"] if block["type"] == "list" else []


# ------------------------------------------------------------------------------------------------- Markdown
def to_markdown(r: dict) -> str:
    cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    out = [f"# {r['title']}", "", f"*Topic:* {r['topic']}  \n*Generated:* {r['generated_at']}", ""]
    if r.get("method", {}).get("warning"):
        out += [f"> **Note:** {r['method']['warning']}", ""]
    out += ["## Executive summary", ""]
    out += [f"- {i['text']} {marks(i['citations'])}".rstrip() for i in r["executive_summary"]] or ["_No evidence was found for this topic._"]
    for sec in r["sections"]:
        out += ["", f"## {sec['title']}", ""]
        for b in sec["blocks"]:
            if b["type"] == "claim":
                out.append(f"- {b['text']} {marks(b['citations'])}" + (" _(limited support)_" if b.get("status") == "weak" else ""))
            elif b["type"] == "list":
                out += [f"- {i['text']} {marks(i['citations'])}".rstrip() for i in b["items"]]
            elif b["type"] == "table":
                out += ["| " + " | ".join(b["columns"]) + " |", "|" + "---|" * len(b["columns"])]
                out += ["| " + " | ".join(cell(c) for c in row["cells"]) + f" {marks(row['citations'])} |" for row in b["rows"]]
            else:
                out += ["", f"_{b['text']}_"]
    out += ["", "## Documents analysed", ""]
    out += [f"- {d['title']} ({d['filename']}), versions {d['versions']}, pages seen {d['pages'] or 'n/a'}" for d in r["documents"]] or ["_None_"]
    out += ["", "## Coverage notes", ""] + [f"- {n}" for n in r["coverage_notes"]]
    out += ["", "## Sources", ""]
    for c in r["citations"]:
        out.append(f"{source_line(c)}  ")
        out += [f"> {q}" for q in (c.get("quotes") or ([c["quote"]] if c.get("quote") else []))]
    v = r["verification"]
    out += ["", "## Method and limitations", "", r["method"]["statement"], "",
            f"Claims verified and kept: {v['claims_kept']} of {v['claims_total']} considered; "
            f"unsupported claims removed: {len(v['unsupported_removed'])}; dropped by final numeric check: {len(v['dropped_at_final_check'])}.", "", f"_{DISCLAIMER}_", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------- DOCX
def to_docx(r: dict) -> bytes:
    from docx import Document
    from docx.shared import Pt

    d = Document()
    d.core_properties.title = r["title"][:200]
    d.add_heading(r["title"], 0)
    p = d.add_paragraph()
    p.add_run("Topic: ").bold = True
    p.add_run(r["topic"])
    d.add_paragraph(f"Generated: {r['generated_at']}")
    if r.get("method", {}).get("warning"):
        d.add_paragraph().add_run("Note: " + r["method"]["warning"]).italic = True
    d.add_heading("Executive summary", 1)
    for i in r["executive_summary"] or [{"text": "No evidence was found for this topic.", "citations": []}]:
        d.add_paragraph(f"{i['text']} {marks(i['citations'])}".strip(), style="List Bullet")
    for sec in r["sections"]:
        d.add_heading(sec["title"], 1)
        for b in sec["blocks"]:
            if b["type"] == "claim":
                d.add_paragraph(f"{b['text']} {marks(b['citations'])}" + (" (limited support)" if b.get("status") == "weak" else ""), style="List Bullet")
            elif b["type"] == "list":
                for i in b["items"]:
                    d.add_paragraph(f"{i['text']} {marks(i['citations'])}".strip(), style="List Bullet")
            elif b["type"] == "table":
                t = d.add_table(rows=1, cols=len(b["columns"]))
                t.style = "Table Grid"
                for j, name in enumerate(b["columns"]):
                    t.rows[0].cells[j].text = name
                    for run in t.rows[0].cells[j].paragraphs[0].runs:
                        run.bold = True
                for row in b["rows"]:
                    cells = t.add_row().cells
                    for j, val in enumerate(row["cells"]):
                        cells[j].text = str(val) + (f" {marks(row['citations'])}" if j == len(row["cells"]) - 1 else "")
            else:
                d.add_paragraph().add_run(b["text"]).italic = True
    d.add_heading("Documents analysed", 1)
    for doc in r["documents"]:
        d.add_paragraph(f"{doc['title']} ({doc['filename']}), versions {doc['versions']}, pages seen {doc['pages'] or 'n/a'}", style="List Bullet")
    d.add_heading("Coverage notes", 1)
    for n in r["coverage_notes"]:
        d.add_paragraph(n, style="List Bullet")
    d.add_heading("Sources", 1)
    for c in r["citations"]:
        d.add_paragraph(source_line(c))
        for q in c.get("quotes") or ([c["quote"]] if c.get("quote") else []):
            run = d.add_paragraph().add_run(f"\u201c{q}\u201d")
            run.italic = True
            run.font.size = Pt(9)
    v = r["verification"]
    d.add_heading("Method and limitations", 1)
    d.add_paragraph(r["method"]["statement"])
    d.add_paragraph(f"Claims verified and kept: {v['claims_kept']} of {v['claims_total']} considered; unsupported claims removed: "
                    f"{len(v['unsupported_removed'])}; dropped by final numeric check: {len(v['dropped_at_final_check'])}.")
    d.add_paragraph().add_run(DISCLAIMER).italic = True
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ----------------------------------------------------------------------------------------------------- PDF
_font_cache: dict[str, tuple[str, str, bool]] = {}


def _fonts(configured: str | None) -> tuple[str, str, bool]:
    key = configured or ""
    if key in _font_cache:
        return _font_cache[key]
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for cand in ([configured] if configured else []) + _FONT_CANDIDATES:
        if cand and Path(cand).is_file():
            bold = Path(cand.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"))
            pdfmetrics.registerFont(TTFont("NexusSans", cand))
            pdfmetrics.registerFont(TTFont("NexusSans-Bold", str(bold) if bold.is_file() else cand))
            _font_cache[key] = ("NexusSans", "NexusSans-Bold", True)
            return _font_cache[key]
    _font_cache[key] = ("Helvetica", "Helvetica-Bold", False)
    return _font_cache[key]


def to_pdf(r: dict, font_path: str | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    regular, bold, unicode_ok = _fonts(font_path)

    def t(s: str) -> str:
        s = escape(str(s))
        return s if unicode_ok else s.encode("latin-1", "replace").decode("latin-1")

    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["Normal"], fontName=regular, fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#444444"))
    note = ParagraphStyle("n", parent=body, fontName=regular, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=ss["Heading2"], fontName=bold, fontSize=13, spaceBefore=12, spaceAfter=4)
    title = ParagraphStyle("t", parent=ss["Title"], fontName=bold, fontSize=18)
    width = A4[0] - 4 * cm

    def bullets(lines: list[str]) -> ListFlowable:
        return ListFlowable([ListItem(Paragraph(t(x), body), leftIndent=12) for x in lines], bulletType="bullet", start="\u2022" if unicode_ok else "-")

    story: list = [Paragraph(t(r["title"]), title), Paragraph(t(f"Topic: {r['topic']}"), body), Paragraph(t(f"Generated: {r['generated_at']}"), small)]
    if r.get("method", {}).get("warning"):
        story.append(Paragraph(t("Note: " + r["method"]["warning"]), note))
    story += [Paragraph("Executive summary", h1)]
    story.append(bullets([f"{i['text']} {marks(i['citations'])}".strip() for i in r["executive_summary"]] or ["No evidence was found for this topic."]))
    for sec in r["sections"]:
        story.append(Paragraph(t(sec["title"]), h1))
        pending: list[str] = []

        def flush() -> None:
            if pending:
                story.append(bullets(list(pending)))
                pending.clear()

        for b in sec["blocks"]:
            if b["type"] == "claim":
                pending.append(f"{b['text']} {marks(b['citations'])}" + (" (limited support)" if b.get("status") == "weak" else ""))
            elif b["type"] == "list":
                pending += [f"{i['text']} {marks(i['citations'])}".strip() for i in b["items"]]
            elif b["type"] == "table":
                flush()
                cw = [width * f for f in (0.22, 0.5, 0.16, 0.12)] if len(b["columns"]) == 4 else None
                data = [[Paragraph(t(c), ParagraphStyle("th", parent=small, fontName=bold)) for c in b["columns"]]]
                data += [[Paragraph(t(str(v) + (f" {marks(row['citations'])}" if j == len(row["cells"]) - 1 else "")), small) for j, v in enumerate(row["cells"])]
                         for row in b["rows"]]
                tbl = Table(data, colWidths=cw, repeatRows=1)
                tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                         ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee"))]))
                story += [tbl, Spacer(1, 4)]
            else:
                flush()
                story.append(Paragraph(t(b["text"]), note))
        flush()
    story.append(Paragraph("Documents analysed", h1))
    story.append(bullets([f"{d['title']} ({d['filename']}), versions {d['versions']}, pages seen {d['pages'] or 'n/a'}" for d in r["documents"]] or ["None"]))
    story += [Paragraph("Coverage notes", h1), bullets(r["coverage_notes"] or ["None"]), Paragraph("Sources", h1)]
    for c in r["citations"]:
        story.append(Paragraph(t(source_line(c)), body))
        for q in c.get("quotes") or ([c["quote"]] if c.get("quote") else []):
            story.append(Paragraph(t(f"\u201c{q}\u201d" if unicode_ok else f'"{q}"'), small))
    v = r["verification"]
    story += [Paragraph("Method and limitations", h1), Paragraph(t(r["method"]["statement"]), body),
              Paragraph(t(f"Claims verified and kept: {v['claims_kept']} of {v['claims_total']} considered; unsupported claims removed: "
                          f"{len(v['unsupported_removed'])}; dropped by final numeric check: {len(v['dropped_at_final_check'])}."), body),
              Paragraph(t(DISCLAIMER), note)]

    def footer(canvas, doc) -> None:  # noqa: ANN001
        canvas.saveState()
        canvas.setFont(regular, 8)
        canvas.drawString(2 * cm, 1.2 * cm, "NEXUS AI research report")
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
                      title=r["title"][:200]).build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
