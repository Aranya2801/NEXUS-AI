"""Builds the synthetic sample corpus and a sample report entirely offline (mock LLM + mock embeddings), then writes:
  datasets/sample_documents/transformer_papers/*.pdf   (synthetic papers; every name and number is fictional)
  docs/examples/sample_report.{md,pdf}
Run: python infrastructure/scripts/generate_sample_report.py"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fixtures import make_pdf  # noqa: E402
from tests.test_reports import PAPERS, TOPIC  # noqa: E402


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = Settings(env="test", database_url=f"sqlite+aiosqlite:///{tmp}/t.db", storage_dir=str(tmp / "s"), log_json=False,
                   embedding_dim=64, bcrypt_rounds=4)
    app = create_app(cfg)
    out_docs, out_ex = ROOT / "datasets/sample_documents/transformer_papers", ROOT / "docs/examples"
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=120) as c:
        r = await c.post("/auth/register", json={"organization_name": "Sample Org", "email": "sample@example.com", "password": "sample-password-123"})
        tok = (await c.post("/auth/login", json={"email": "sample@example.com", "password": "sample-password-123"})).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        cid = (await c.post("/collections", json={"name": "papers"}, headers=h)).json()["id"]
        for name, pages in PAPERS.items():
            data = make_pdf(pages)
            (out_docs / name).write_bytes(data)
            await c.post(f"/collections/{cid}/documents", files={"file": (name, data)}, headers=h)
        await app.state.runner.drain()
        rid = (await c.post("/reports", json={"topic": TOPIC}, headers=h)).json()["id"]
        await app.state.reports.drain()
        for fmt in ("md", "pdf"):
            (out_ex / f"sample_report.{fmt}").write_bytes((await c.get(f"/reports/{rid}/export", params={"format": fmt}, headers=h)).content)
    print("wrote", sorted(p.name for p in out_docs.iterdir()), "and docs/examples/sample_report.{md,pdf}")


asyncio.run(main())
