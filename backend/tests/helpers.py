"""Shared API helpers for integration-style tests."""
from __future__ import annotations

PW = "correct-horse-battery"

DOCS = {
    "attention.txt": ("Self-attention lets a transformer relate every token to every other token. "
                      "Multi-head attention improves representation quality in transformer models. " * 12),
    "kubernetes.txt": ("Kubernetes schedules container workloads across a cluster of nodes. "
                       "Container orchestration automates deployment, scaling and rollout of pods. " * 12),
    "billing.txt": ("Invoice reconciliation failed with error code zyxwv42 during the monthly billing run. "
                    "Finance must reconcile ledger entries before the audit deadline. " * 12),
}


async def register(client, org: str, email: str) -> dict:
    r = await client.post("/auth/register", json={"organization_name": org, "email": email, "password": PW})
    assert r.status_code == 201, r.text
    return await login(client, email)


async def login(client, email: str) -> dict:
    r = await client.post("/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def make_collection(client, h, name="kb", restricted=False) -> str:
    r = await client.post("/collections", json={"name": name, "is_restricted": restricted}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def upload(client, app, h, cid, name, data, department=None):
    r = await client.post(f"/collections/{cid}/documents", files={"file": (name, data)},
                          data={"department": department} if department else None, headers=h)
    assert r.status_code == 202, r.text
    await app.state.runner.drain()
    return r.json()


async def seed(client, app, h, cid, department=None) -> dict[str, str]:
    ids = {}
    for name, text in DOCS.items():
        ids[name] = (await upload(client, app, h, cid, name, text.encode(), department))["document"]["id"]
    return ids


async def search(client, h, query, **kw):
    r = await client.post("/search", json={"query": query, **kw}, headers=h)
    return r
