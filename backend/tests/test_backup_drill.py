"""Phase 18: a real backup -> verify -> restore drill against PostgreSQL + pgvector (bundled server). Proves the procedure and the script, not any production setup."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.helpers import make_collection, register, search, seed
from tests.test_postgres_integration import app, client, pg  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _client_tools_match_the_bundled_server(monkeypatch):
    """Use the client tools that ship with the bundled test server (same major version); a system pg_restore of another version is rejected by the tool on purpose."""
    import pixeltable_pgserver
    monkeypatch.setenv("PG_BIN", str(Path(pixeltable_pgserver.__file__).parent / "pginstall" / "bin"))

spec = importlib.util.spec_from_file_location("backup_tool", ROOT / "infrastructure" / "scripts" / "backup.py")
B = importlib.util.module_from_spec(spec)
sys.modules["backup_tool"] = B
spec.loader.exec_module(B)


def test_dsn_handling_keeps_passwords_out_of_argv():
    dsn, env = B.normalise_dsn("postgresql+asyncpg://nexus:p%40ss%2Fw@db.example.com:5432/nexus?sslmode=require")
    assert dsn == "postgresql://nexus@db.example.com:5432/nexus?sslmode=require" and env == {"PGPASSWORD": "p@ss/w"} and "p%40ss" not in dsn
    assert B.normalise_dsn("postgresql+asyncpg://postgres@/db?host=/tmp/x") == ("postgresql://postgres@/db?host=/tmp/x", {})
    assert B.with_db("postgresql://u@h/prod?x=1", "postgres") == "postgresql://u@h/postgres?x=1" and B.db_name("postgresql://u@h/prod") == "prod"


async def test_backup_verify_and_restore_drill_roundtrip(app, client, tmp_path):
    h = await register(client, "Acme", "a@example.com")
    ids = await seed(client, app, h, await make_collection(client, h))
    assert ids and (await search(client, h, "kubernetes container orchestration", top_k=3)).json()["hits"]
    url = app.state.settings.database_url
    storage = tmp_path / "s"
    out = tmp_path / "bk"
    m = B.create_backup(url, out, storage_dir=storage if storage.exists() else None, deep=True)
    assert m["state"]["tables"]["chunks"] > 0 and m["state"]["tables"]["users"] == 1 and "fingerprints" in m["state"]
    assert m["state"]["alembic_revision"] is None            # the test app builds its schema with create_all, not alembic
    mpath = Path(m["manifest_path"])
    v = B.verify_backup(mpath)
    assert v["ok"], v

    # the live database changes after the backup (a 'disaster'): the drill must restore the *backup's* state, not the current one
    await client.delete(f"/documents/{next(iter(ids.values()))}", headers=h)
    r = B.restore_drill(mpath, url)
    assert r["ok"], r
    assert r["rows_in_backup"] == sum(m["state"]["tables"].values()) and r["pgvector"] and r["vector_search_rows"] > 0
    assert {"ix_chunks_fts", "ix_chunks_embedding_hnsw"} <= set(r["chunk_indexes"]) and r["restore_seconds"] >= 0
    # the scratch database was dropped
    assert r["scratch_database"] not in B.psql(B.normalise_dsn(B.with_db(url, "postgres"))[0], {}, "SELECT datname FROM pg_database")


async def test_corrupted_backup_is_refused_and_row_loss_is_detected(app, client, tmp_path):
    h = await register(client, "Acme", "a@example.com")
    await seed(client, app, h, await make_collection(client, h))
    m = B.create_backup(app.state.settings.database_url, tmp_path / "bk")
    mpath = Path(m["manifest_path"])
    dump = mpath.parent / m["dump"]
    # 1) bit-rot / tampering -> checksum mismatch, and the drill refuses to run
    raw = bytearray(dump.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    dump.write_bytes(bytes(raw))
    v = B.verify_backup(mpath)
    assert not v["ok"] and "checksum" in v["problems"][0]
    with pytest.raises(B.BackupError, match="failed verification"):
        B.restore_drill(mpath, app.state.settings.database_url)
    # 2) a manifest claiming rows the dump does not contain is caught by the count comparison
    m2 = B.create_backup(app.state.settings.database_url, tmp_path / "bk2")
    p2 = Path(m2["manifest_path"])
    data = json.loads(p2.read_text())
    data["state"]["tables"]["chunks"] += 5
    p2.write_text(json.dumps(data))
    r = B.restore_drill(p2, app.state.settings.database_url)
    assert not r["ok"] and any("row count mismatch in chunks" in p for p in r["problems"])


def test_missing_files_and_missing_tools_are_reported(tmp_path, monkeypatch):
    mf = tmp_path / "m.json"
    mf.write_text(json.dumps({"dump": "gone.dump", "dump_sha256": "x", "created_at": "t", "state": {"tables": {}}}))
    assert B.verify_backup(mf)["problems"] == ["dump file missing"]
    monkeypatch.setenv("PG_BIN", str(tmp_path))
    monkeypatch.setattr(B.shutil, "which", lambda n: None)
    monkeypatch.setitem(sys.modules, "pixeltable_pgserver", None)
    with pytest.raises(B.BackupError, match="not found"):
        B.find_bin("pg_dump")


def test_cli_exit_codes(tmp_path, capsys):
    assert B.main(["verify", "--manifest", str(tmp_path / "nope.json")]) == 2          # unreadable manifest -> usage/environment error
    assert "cannot read manifest" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert B.main(["verify", "--manifest", str(bad)]) == 2                              # malformed manifest
    gone = tmp_path / "g.json"
    gone.write_text(json.dumps({"dump": "x.dump", "dump_sha256": "x", "created_at": "t", "state": {"tables": {}}}))
    assert B.main(["verify", "--manifest", str(gone)]) == 1                             # readable manifest, failed verification


def test_storage_archive_is_checksummed(tmp_path):
    # exercised without a database: archive creation and verification logic for the local-filesystem backend
    storage = tmp_path / "storage"
    (storage / "org1").mkdir(parents=True)
    (storage / "org1" / "a.bin").write_bytes(b"hello")
    arc = tmp_path / "x.tar.gz"
    import tarfile
    with tarfile.open(arc, "w:gz") as tf:
        tf.add(storage, arcname="storage")
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"not a real dump")
    mf = tmp_path / "m.json"
    mf.write_text(json.dumps({"dump": "d.dump", "dump_sha256": B.sha256_file(dump), "created_at": "t", "state": {"tables": {}},
                              "storage": {"archive": "x.tar.gz", "archive_sha256": "0" * 64, "files": {}}}))
    v = B.verify_backup(mf)
    assert not v["ok"] and any("storage archive" in p for p in v["problems"])


def test_mismatched_client_version_is_refused_with_a_clear_message(monkeypatch):
    monkeypatch.setattr(B, "psql", lambda *a, **k: "16.4")
    monkeypatch.setattr(B, "_run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "pg_restore (PostgreSQL) 17.2\n", ""))
    monkeypatch.setattr(B, "find_bin", lambda n: n)
    with pytest.raises(B.BackupError, match=r"pg_dump is PostgreSQL 17 but the server is 16"):
        B.check_client_matches_server("postgresql://x/y", {})
    monkeypatch.setattr(B, "_run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "pg_dump (PostgreSQL) 16.9\n", ""))
    B.check_client_matches_server("postgresql://x/y", {})            # same major: fine
    assert B._major("17beta1") == 17
    with pytest.raises(B.BackupError):
        B._major("none")
