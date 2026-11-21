import pytest

from app.core.exceptions import IngestionError, UnsupportedFormat, ValidationFailed
from app.document_processing.chunking import chunk_document
from app.document_processing.normalize import (
    detect_language, fingerprint, normalize_text, sketch_similarity,
)
from app.document_processing.parsers import parse_document
from app.document_processing.types import Block, ParsedDocument
from app.ingestion.validation import sanitize_filename, validate_upload
from tests.fixtures import make_docx, make_pdf, make_pptx, make_xlsx


def test_normalize_dehyphenates_and_collapses():
    assert normalize_text("inter-\nnational   trade\r\n\r\n\r\n\r\nend") == "international trade\n\nend"


def test_language_detection():
    assert detect_language("The model is trained on the data and the results are in the table for this work") == "en"
    assert detect_language("El modelo de la red neuronal se entrena con los datos de la empresa para el análisis") == "es"
    assert detect_language("ab") == "und"


def test_near_duplicate_sketch():
    a = " ".join(f"word{i}" for i in range(300))
    b = a + " extra tail words appended here now"
    c = " ".join(f"other{i}" for i in range(300))
    assert sketch_similarity(fingerprint(a), fingerprint(b)) > 0.8
    assert sketch_similarity(fingerprint(a), fingerprint(c)) < 0.1


def test_pdf_pages_sections_and_running_header_removed():
    pages = [["ACME CONFIDENTIAL REPORT", "1. INTRODUCTION", "Transformers use attention. They scale well.", "Page 1"],
             ["ACME CONFIDENTIAL REPORT", "Further text continues here on page two.", "Page 2"],
             ["ACME CONFIDENTIAL REPORT", "2. METHODS", "We train with Adam optimiser.", "Page 3"]]
    doc = parse_document("r.pdf", make_pdf(pages))
    joined = " ".join(b.text for b in doc.blocks)
    assert "CONFIDENTIAL" not in joined and "Page 2" not in joined
    by_text = {b.text: b for b in doc.blocks}
    assert by_text["Transformers use attention. They scale well."].page == 1
    assert by_text["Transformers use attention. They scale well."].section == "1. INTRODUCTION"
    assert by_text["We train with Adam optimiser."].page == 3
    assert doc.metadata["page_count"] == 3 and doc.language


def test_docx_sections_and_tables():
    doc = parse_document("s.docx", make_docx())
    kinds = {(b.kind, b.section) for b in doc.blocks}
    assert ("table", "Results") in kinds and ("text", "Transformer Survey") in kinds
    assert any("27.3" in b.text for b in doc.blocks if b.kind == "table")


def test_pptx_pages_are_slide_numbers():
    doc = parse_document("d.pptx", make_pptx())
    assert [(b.page, b.section) for b in doc.blocks] == [(1, "Motivation"), (2, "Limitations")]


def test_xlsx_and_csv_rows_are_labelled():
    x = parse_document("m.xlsx", make_xlsx())
    assert "model: hybrid; precision: 0.72; recall: 0.66" in x.blocks[0].text and x.blocks[0].section == "Sheet Metrics"
    c = parse_document("m.csv", b"a,b\n1,2\n3,4\n")
    assert c.blocks[0].text.splitlines() == ["a: 1; b: 2", "a: 3; b: 4"]


def test_html_and_markdown():
    h = parse_document("p.html", b"<html><head><title>T</title><script>evil()</script></head><body><h1>Intro</h1><p>Hello world.</p></body></html>")
    assert h.title == "T" and h.blocks[0].section == "Intro" and "evil" not in h.blocks[0].text
    m = parse_document("n.md", b"# Top\npara one\n\n## Sub\npara two")
    assert [(b.section, b.text) for b in m.blocks] == [("Top", "para one"), ("Sub", "para two")]


def test_corrupt_and_unsupported():
    with pytest.raises(IngestionError):
        parse_document("x.docx", b"PK\x03\x04garbage")
    with pytest.raises(UnsupportedFormat):
        parse_document("x.exe", b"MZ")


def _long_doc():
    sents = [f"Sentence number {i} discusses retrieval topic {i % 7} in some detail." for i in range(120)]
    blocks = [Block(" ".join(sents[:60]), 1, "A"), Block(" ".join(sents[60:]), 2, "B")]
    return ParsedDocument("t", blocks, "txt")


def test_chunks_are_traceable_overlapping_and_section_bound():
    cs = chunk_document(_long_doc(), target_tokens=60, overlap_tokens=15, parent_tokens=200)
    assert len(cs.children) > 4 and cs.parents
    for ch in cs.children:
        assert ch.token_count <= 60 + 1
        assert ch.section in {"A", "B"} and ch.page_start == ch.page_end == (1 if ch.section == "A" else 2)
        assert cs.full_text[ch.char_start : ch.char_end].startswith(ch.text.split(".")[0][:20])
        assert cs.parents[ch.parent_ordinal].section == ch.section
    a = [c for c in cs.children if c.section == "A"]
    assert a[1].char_start < a[0].char_end  # overlap
    assert all(p.char_end > p.char_start for p in cs.parents)


def test_oversized_sentence_is_hard_split_and_semantic_mode_runs():
    big = Block(" ".join(["word"] * 500) + ".", 1, "S")
    cs = chunk_document(ParsedDocument("t", [big], "txt"), target_tokens=100, overlap_tokens=10)
    assert all(c.token_count <= 100 for c in cs.children) and len(cs.children) >= 5
    sem = chunk_document(_long_doc(), target_tokens=60, overlap_tokens=10, semantic=True)
    assert sem.children


def test_validation(settings):
    assert validate_upload("../../etc/passwd.txt", b"hello", settings) == "passwd.txt"
    assert sanitize_filename("a b<>.txt") == "a b__.txt"
    with pytest.raises(UnsupportedFormat):
        validate_upload("a.exe", b"MZ", settings)
    with pytest.raises(ValidationFailed):
        validate_upload("a.pdf", b"not a pdf", settings)
    with pytest.raises(ValidationFailed):
        validate_upload("a.docx", b"PK\x03\x04junk", settings)
    with pytest.raises(ValidationFailed):
        validate_upload("a.txt", b"abc\x00def", settings)
    with pytest.raises(ValidationFailed):
        validate_upload("a.txt", b"", settings)
    assert validate_upload("a.docx", make_docx(), settings) == "a.docx"
    assert validate_upload("a.pdf", make_pdf([["hi"]]), settings) == "a.pdf"
