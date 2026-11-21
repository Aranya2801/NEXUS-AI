import io
import re
from datetime import datetime, timezone

import pytest
from docx import Document as DocxDocument
from pypdf import PdfReader
from sqlalchemy import delete, select

from app.agents.textutil import numbers
from app.database.models import Chunk, CollectionAccess, Report, User
from app.reporting import export
from tests.fixtures import make_pdf
from tests.helpers import PW, login, make_collection, register, upload
from tests.test_workflow import RouterLLM, running

PAPERS = {
    "alpha.pdf": [["1. METHODS", "The Alpha Transformer uses sparse attention over local windows.", "We train on the WikiText dataset using the Adam optimizer."],
                  ["2. RESULTS", "Alpha Transformer reaches perplexity of 18.3 on WikiText.", "3. LIMITATIONS",
                   "Alpha Transformer suffers from quadratic memory on very long sequences.", "Future work remains open for hardware efficient kernels."]],
    "beta.pdf": [["1. METHODS", "The Beta Transformer builds on the Alpha Transformer with linear attention.", "We evaluate the Beta Transformer on the WikiText dataset."],
                 ["2. RESULTS", "Beta Transformer reaches perplexity of 21.9 on WikiText.", "3. LIMITATIONS", "A limitation of the Beta Transformer is reduced accuracy on copy tasks.",
                  "Whether linear attention scales to billions of parameters is an open problem."]],
    "gamma.pdf": [["1. METHODS", "The Gamma Transformer uses convolutional token mixing and the C4 corpus."],
                  ["2. RESULTS", "Gamma Transformer reaches accuracy of 71.2 percent on the GLUE benchmark."]],
}
TOPIC = "transformer architectures"


async def papers(app, c, h, cid):
    return {n: (await upload(c, app, h, cid, n, make_pdf(pg)))["document"]["id"] for n, pg in PAPERS.items()}


async def make_report(app, c, h, **body):
    r = await c.post("/reports", json={"topic": TOPIC, **body}, headers=h)
    assert r.status_code == 202, r.text
    await app.state.reports.drain()
    return (await c.get(f"/reports/{r.json()['id']}", headers=h)).json()


def all_blocks(rep):
    for s in rep["content"]["sections"]:
        for b in s["blocks"]:
            yield s["id"], b


def text_of(b):
    if b["type"] == "claim":
        return [(b["text"], b["citations"])]
    if b["type"] == "list":
        return [(i["text"], i["citations"]) for i in b["items"]]
    if b["type"] == "table":
        return [(" ".join(r["cells"][1:3]), r["citations"]) for r in b["rows"]]
    return []


# ------------------------------------------------------------------ content
async def test_report_is_structured_page_cited_and_complete(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Acme", "a@example.com")
        ids = await papers(app, c, h, await make_collection(c, h))
        rep = await make_report(app, c, h)
        assert rep["status"] == "ready" and rep["error"] is None
        assert [p["step"] for p in rep["progress"]] == ["discover", "methodologies", "datasets", "results", "limitations", "contradictions", "gaps"]
        rc = rep["content"]
        assert [s["id"] for s in rc["sections"]] == ["methodologies", "datasets", "results", "limitations", "contradictions", "gaps"]
        assert {d["document_id"] for d in rc["documents"]} == set(ids.values()) and rc["executive_summary"]
        assert rc["method"]["warning"] and "mock" in rc["method"]["warning"]            # honest about the offline mock
        cites = {x["id"]: x for x in rc["citations"]}
        assert cites and all(x["page_start"] in (1, 2) and x["quote"] and x["title"] for x in cites.values())
        sec = {s["id"]: s for s in rc["sections"]}
        joined = lambda sid: " ".join(t for b in sec[sid]["blocks"] for t, _ in text_of(b))  # noqa: E731
        assert "perplexity" not in joined("methodologies") and "uses sparse attention" in joined("methodologies")
        assert "WikiText dataset" in joined("datasets") and "GLUE benchmark" in joined("datasets")
        table = next(b for b in sec["results"]["blocks"] if b["type"] == "table")
        assert {r["cells"][2] for r in table["rows"]} == {"18.3", "21.9", "71.2"} and all(r["cells"][3] == "2" for r in table["rows"])
        assert "quadratic memory" in joined("limitations") and "copy tasks" in joined("limitations")
        assert "Future work remains open" in joined("gaps") and "open problem" in joined("gaps")
        contra = joined("contradictions")
        assert "18.3" in contra and "21.9" in contra and any("Heuristic" in b.get("text", "") for b in sec["contradictions"]["blocks"])
        for _, b in all_blocks(rep):                                                       # every statement cites existing sources
            for _, cs in text_of(b):
                assert cs and all(i in cites for i in cs)


async def test_nothing_in_a_report_is_invented(tmp_path):
    """Property check against the database: each quote is verbatim from its chunk and every number in a block occurs in the chunks it cites."""
    async with running(tmp_path) as (app, c):
        h = await register(c, "Acme", "a@example.com")
        await papers(app, c, h, await make_collection(c, h))
        rep = await make_report(app, c, h)
        cites = {x["id"]: x for x in rep["content"]["citations"]}
        norm = lambda s: " ".join(s.split())  # noqa: E731
        async with app.state.session_factory() as db:
            chunks = {ch.id: ch for ch in (await db.execute(select(Chunk).where(Chunk.id.in_([x["chunk_id"] for x in cites.values()])))).scalars()}
        assert len(chunks) == len(cites)
        for cit in cites.values():
            ch = chunks[cit["chunk_id"]]
            assert ch.is_current and cit["page_start"] == ch.page_start
            assert all(norm(q) in norm(ch.text) for q in cit["quotes"]), cit
        n_checked = 0
        for _, b in all_blocks(rep):
            for text, cs in text_of(b):
                src = set().union(*(numbers(chunks[cites[i]["chunk_id"]].text) for i in cs))
                assert numbers(text) <= src, (text, cs)
                n_checked += 1
        assert n_checked >= 8


async def test_fabricated_model_output_never_reaches_the_report(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Acme", "a@example.com")
        await papers(app, c, h, await make_collection(c, h))
        def hostile(user):
            claims = [{"text": "Alpha Transformer reaches perplexity of 5.1 on WikiText.", "citations": [1]},      # invented number
                      {"text": "The Alpha Transformer was invented in 2031 by aliens.", "citations": []}]          # no citation
            real = next((int(m.group(1)) for m in re.finditer(r'<passage id="(\d+)"[^>]*>\n(.*?)\n</passage>', user, re.S)
                         if "uses sparse attention" in m.group(2)), None)
            if real:                                                                                             # one genuinely supported claim
                claims.append({"text": "The Alpha Transformer uses sparse attention over local windows.", "citations": [real]})
            return {"claims": claims}

        app.state.agent_deps.llm = RouterLLM(app.state.llm, answer=hostile)
        rep = await make_report(app, c, h)
        md = (await c.get(f"/reports/{rep['id']}/export", params={"format": "md"}, headers=h)).text
        assert "5.1" not in md and "aliens" not in md and "2031" not in md
        assert rep["content"]["verification"]["unsupported_removed"] and rep["content"]["method"]["warning"] is None
        assert "uses sparse attention over local windows" in md


async def test_report_survives_a_dead_llm_with_extractive_fallback(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Acme", "a@example.com")
        await papers(app, c, h, await make_collection(c, h))
        app.state.agent_deps.llm = RouterLLM(app.state.llm, answer=ConnectionError("llm down"))
        rep = await make_report(app, c, h)
        assert rep["status"] == "ready" and rep["content"]["verification"]["claims_kept"] >= 1
        cites = {x["id"] for x in rep["content"]["citations"]}
        assert all(cs and set(cs) <= cites for _, b in all_blocks(rep) for _, cs in text_of(b))


async def test_unrelated_topic_yields_an_honest_empty_report(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Acme", "a@example.com")
        await papers(app, c, h, await make_collection(c, h))
        rep = await make_report(app, c, h, topic="quantum gravity loop spin foam")
        rc = rep["content"]
        assert rep["status"] == "ready" and rc["documents"] == [] and rc["sections"] == [] and rc["citations"] == [] and rc["executive_summary"] == []
        assert "No documents accessible to you matched" in rc["coverage_notes"][0]
        md = (await c.get(f"/reports/{rep['id']}/export", params={"format": "md"}, headers=h)).text
        assert "No evidence was found" in md


async def test_scope_parameters(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Acme", "a@example.com")
        c1, c2 = await make_collection(c, h, "one"), await make_collection(c, h, "two")
        a_id = (await upload(c, app, h, c1, "alpha.pdf", make_pdf(PAPERS["alpha.pdf"])))["document"]["id"]
        b_id = (await upload(c, app, h, c2, "beta.pdf", make_pdf(PAPERS["beta.pdf"])))["document"]["id"]
        await upload(c, app, h, c2, "gamma.pdf", make_pdf(PAPERS["gamma.pdf"]))
        only_c1 = await make_report(app, c, h, collection_ids=[c1])
        assert [d["document_id"] for d in only_c1["content"]["documents"]] == [a_id]
        pinned = await make_report(app, c, h, document_ids=[b_id])
        assert [d["document_id"] for d in pinned["content"]["documents"]] == [b_id]
        capped = await make_report(app, c, h, max_documents=2)
        assert len(capped["content"]["documents"]) == 2


# ------------------------------------------------------------------ authorisation and redaction
async def test_report_visibility_and_access_revocation_redaction(tmp_path):
    async with running(tmp_path) as (app, c):
        ha = await register(c, "OrgA", "a@example.com")
        hb = await register(c, "OrgB", "b@example.com")
        restricted = await make_collection(c, ha, "secret", restricted=True)
        await papers(app, c, ha, restricted)
        await c.post("/auth/users", json={"email": "e@example.com", "password": PW, "role": "editor"}, headers=ha)
        await c.post("/auth/users", json={"email": "f@example.com", "password": PW, "role": "editor"}, headers=ha)
        he, hf = await login(c, "e@example.com"), await login(c, "f@example.com")
        uid = (await c.get("/auth/me", headers=he)).json()["id"]
        await c.post(f"/collections/{restricted}/access", json={"user_id": uid}, headers=ha)

        rep = await make_report(app, c, he)
        rid = rep["id"]
        assert rep["status"] == "ready" and rep["content"]["citations"]
        # visibility: other org / other non-admin user -> 404; org admin -> allowed; list is scoped
        assert (await c.get(f"/reports/{rid}", headers=hb)).status_code == 404
        assert (await c.get(f"/reports/{rid}", headers=hf)).status_code == 404
        assert (await c.get(f"/reports/{rid}/export", headers=hf)).status_code == 404
        assert (await c.delete(f"/reports/{rid}", headers=hf)).status_code == 404
        assert (await c.get(f"/reports/{rid}", headers=ha)).status_code == 200
        assert [r["id"] for r in (await c.get("/reports", headers=he)).json()] == [rid] and (await c.get("/reports", headers=hf)).json() == []
        assert "Transformer" in (await c.get(f"/reports/{rid}/export", params={"format": "md"}, headers=he)).text
        # a user with no access to the restricted collection generates nothing from it
        assert (await make_report(app, c, hf))["content"]["documents"] == []

        # revoke access: the stored report must stop revealing the restricted documents, in JSON and in every export format
        async with app.state.session_factory() as db:
            await db.execute(delete(CollectionAccess).where(CollectionAccess.user_id == uid))
            await db.commit()
        red = (await c.get(f"/reports/{rid}", headers=he)).json()["content"]
        assert red["redacted"] and red["documents"] == [] and all(x["hidden"] and x["quote"] == "[source no longer accessible]" for x in red["citations"])
        flat = repr(red)
        assert "quadratic memory" not in flat and "perplexity" not in flat and "alpha.pdf" not in flat
        md = (await c.get(f"/reports/{rid}/export", params={"format": "md"}, headers=he)).text
        assert "quadratic memory" not in md and "perplexity" not in md and "no longer accessible" in md
        pdf = "".join(p.extract_text() for p in PdfReader(io.BytesIO((await c.get(f"/reports/{rid}/export", headers=he)).content)).pages)
        assert "quadratic memory" not in pdf and "21.9" not in pdf
        docx = DocxDocument(io.BytesIO((await c.get(f"/reports/{rid}/export", params={"format": "docx"}, headers=he)).content))
        assert "quadratic memory" not in "\n".join(p.text for p in docx.paragraphs)
        assert "Transformer" in repr((await c.get(f"/reports/{rid}", headers=ha)).json()["content"])        # admin still has access


# ------------------------------------------------------------------ exports
async def test_exports_roundtrip_markdown_docx_pdf(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Acme", "a@example.com")
        await papers(app, c, h, await make_collection(c, h))
        rep = await make_report(app, c, h, title='../../evil <b>"report"</b>.pdf')
        rid = rep["id"]
        for fmt, ctype in (("md", "text/markdown"), ("docx", "officedocument.wordprocessingml"), ("pdf", "application/pdf")):
            r = await c.get(f"/reports/{rid}/export", params={"format": fmt}, headers=h)
            assert r.status_code == 200 and ctype in r.headers["content-type"] and r.headers["cache-control"] == "no-store"
            fn = re.search(r'filename="([^"]+)"', r.headers["content-disposition"]).group(1)
            assert fn.endswith("." + fmt) and not set(fn) & set('/\\<>"') and ".." not in fn.replace("._", "")
        md = (await c.get(f"/reports/{rid}/export", params={"format": "md"}, headers=h)).text
        assert md.startswith("# ../../evil") and "## Sources" in md and "p.2" in md and "[1]" in md and "| Document | Reported result (verbatim) |" in md
        d = DocxDocument(io.BytesIO((await c.get(f"/reports/{rid}/export", params={"format": "docx"}, headers=h)).content))
        heads = [p.text for p in d.paragraphs if p.style.name.startswith("Heading")]
        assert {"Executive summary", "Reported Quantitative Results", "Sources", "Method and limitations"} <= set(heads)
        assert [x.text for x in d.tables[0].rows[0].cells] == ["Document", "Reported result (verbatim)", "Numbers", "Page"]
        assert {"18.3", "21.9", "71.2"} <= {row.cells[2].text for row in d.tables[0].rows[1:]}
        assert any(p.text.startswith("[1]") and "p." in p.text for p in d.paragraphs)
        pdf = PdfReader(io.BytesIO((await c.get(f"/reports/{rid}/export", params={"format": "pdf"}, headers=h)).content))
        text = "\n".join(p.extract_text() for p in pdf.pages)
        assert "Executive summary" in text and "21.9" in text and "p.2" in text and "Page 1" in text and "NEXUS AI research report" in text
        assert (await c.get(f"/reports/{rid}/export", params={"format": "exe"}, headers=h)).status_code == 422


def test_pdf_font_fallback_and_unicode(monkeypatch):
    rep = {"title": "Café 日本語", "topic": "naïve", "generated_at": "now", "documents": [], "coverage_notes": [], "executive_summary": [{"text": "Zażółć gęślą 日本語", "citations": [1]}],
           "sections": [], "citations": [{"id": 1, "title": "T", "filename": "f", "page_start": 1, "page_end": 1, "section": None, "version": 1, "quote": "q", "quotes": ["q"]}],
           "verification": {"claims_kept": 0, "claims_total": 0, "unsupported_removed": [], "dropped_at_final_check": []}, "method": {"statement": "s", "warning": None}}
    monkeypatch.setattr(export, "_FONT_CANDIDATES", [])
    export._font_cache.clear()
    pdf = PdfReader(io.BytesIO(export.to_pdf(rep)))                    # Helvetica fallback: must not crash, unsupported glyphs become '?'
    assert "?" in pdf.pages[0].extract_text() and "Executive summary" in pdf.pages[0].extract_text()
    export._font_cache.clear()
    monkeypatch.undo()
    export._font_cache.clear()
    if any(__import__("pathlib").Path(p).is_file() for p in export._FONT_CANDIDATES):
        txt = PdfReader(io.BytesIO(export.to_pdf(rep))).pages[0].extract_text()
        assert "Zażółć" in txt                                         # Unicode survives when a TTF font is available


# ------------------------------------------------------------------ failure handling
async def test_failed_report_is_reported_and_restart_recovery(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Acme", "a@example.com")
        await papers(app, c, h, await make_collection(c, h))

        async def boom(*a, **k):
            raise ValueError("pipeline exploded")

        real = app.state.pipeline.run
        app.state.pipeline.run = boom
        rep = await make_report(app, c, h)
        assert rep["status"] == "failed" and "ValueError" in rep["error"] and rep["content"] is None
        assert (await c.get(f"/reports/{rep['id']}/export", headers=h)).status_code == 409
        app.state.pipeline.run = real
        assert (await make_report(app, c, h))["status"] == "ready"                  # recovers on the next request
        async with app.state.session_factory() as db:                               # a report left running by a "crashed" process
            user = (await db.execute(select(User))).scalars().first()
            db.add(Report(org_id=user.org_id, user_id=user.id, title="orphan", topic="x", status="running", params={}, progress=[]))
            await db.commit()
    async with running(tmp_path) as (app2, c2):
        h2 = await login(c2, "a@example.com")
        orphan = next(r for r in (await c2.get("/reports", headers=h2)).json() if r["title"] == "orphan")
        got = (await c2.get(f"/reports/{orphan['id']}", headers=h2)).json()
        assert got["status"] == "failed" and "interrupted" in got["error"]


async def test_report_api_validation_and_concurrency(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Acme", "a@example.com")
        await papers(app, c, h, await make_collection(c, h))
        assert (await c.post("/reports", json={"topic": "ab"}, headers=h)).status_code == 422
        assert (await c.post("/reports", json={"topic": TOPIC, "max_documents": 99}, headers=h)).status_code == 422
        assert (await c.post("/reports", json={"topic": TOPIC})).status_code == 401
        ids = [(await c.post("/reports", json={"topic": TOPIC}, headers=h)).json()["id"] for _ in range(3)]
        await app.state.reports.drain()
        assert [(await c.get(f"/reports/{i}", headers=h)).json()["status"] for i in ids] == ["ready"] * 3
        assert (await c.delete(f"/reports/{ids[0]}", headers=h)).status_code == 204 and (await c.get(f"/reports/{ids[0]}", headers=h)).status_code == 404
