#!/usr/bin/env python3
"""Logical backup, verification and restore drill for NEXUS (PostgreSQL + pgvector, optional local document storage).

  backup.py backup --out DIR [--dsn URL] [--storage-dir DIR] [--deep]   pg_dump (custom format) + manifest (sha256, alembic revision, row counts[, row fingerprints])
  backup.py verify --manifest FILE                                        checksum + `pg_restore --list` readability (+ storage archive checksum)
  backup.py drill  --manifest FILE [--admin-dsn URL] [--keep]             restores into a scratch database, compares counts/fingerprints/revision, checks
                                                                          pgvector search and indexes, drops the scratch DB, prints a JSON report (non-zero exit on any mismatch)

What this is and is not: a *logical* snapshot taken with pg_dump (consistent, portable across PG minor versions, includes pgvector data). It is NOT point-in-time recovery – for that use
your managed database's PITR (see deploy/terraform and docs/disaster-recovery.md). With an S3/MinIO storage backend the document blobs are protected by bucket versioning/replication, not by this
script; `--storage-dir` only covers the local-filesystem backend. The drill proves a backup can be restored and is internally consistent; the timings it prints are for YOUR data on YOUR hardware only.
Passwords are passed to the PostgreSQL tools through the environment, never on the command line."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

FORMAT_VERSION = 1


class BackupError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ plumbing
def find_bin(name: str) -> str:
    d = os.environ.get("PG_BIN")
    if d and (Path(d) / name).exists():
        return str(Path(d) / name)
    if shutil.which(name):
        return shutil.which(name)  # type: ignore[return-value]
    try:                                                    # dev/CI convenience: the bundled server used by the test-suite
        import pixeltable_pgserver
        p = Path(pixeltable_pgserver.__file__).parent / "pginstall" / "bin" / name
        if p.exists():
            return str(p)
    except ImportError:
        pass
    raise BackupError(f"{name} not found: install PostgreSQL client tools or set PG_BIN")


def _major(text: str) -> int:
    m = re.search(r"(\d+)(?:\.\d+)*", text)
    if not m:
        raise BackupError(f"cannot parse a PostgreSQL version from {text!r}")
    return int(m.group(1))


def check_client_matches_server(dsn: str, env: dict[str, str], tools: tuple[str, ...] = ("pg_dump", "pg_restore")) -> None:
    """pg_dump/pg_restore must be the same major version as the server they talk to: a newer client writes SQL the older server rejects
    (e.g. PG17 emits `SET transaction_timeout`, unknown to PG16), and an older one cannot dump a newer server. Fail early with a fix, not midway through a restore."""
    server = _major(psql(dsn, env, "SHOW server_version"))
    for t in tools:
        client = _major(_run([find_bin(t), "--version"], {}, f"{t} --version").stdout)
        if client != server:
            raise BackupError(f"{t} is PostgreSQL {client} but the server is {server}: install matching client tools or point PG_BIN at them")


def normalise_dsn(url: str) -> tuple[str, dict[str, str]]:
    """`postgresql+asyncpg://u:pw@h/db?x=y` -> (`postgresql://u@h/db?x=y`, {"PGPASSWORD": pw}). The password never appears in argv."""
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    s = urlsplit(url)
    env = {}
    netloc = s.netloc
    if s.password:
        env["PGPASSWORD"] = unquote(s.password)
        host = s.hostname or ""
        netloc = (quote(unquote(s.username or ""), safe="") + "@" if s.username else "") + (f"[{host}]" if ":" in host else host) + (f":{s.port}" if s.port else "")
    return urlunsplit((s.scheme, netloc, s.path, s.query, s.fragment)), env


def with_db(url: str, db: str) -> str:
    s = urlsplit(url)
    return urlunsplit((s.scheme, s.netloc, "/" + db, s.query, s.fragment))


def db_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _run(args: list[str], env_extra: dict[str, str], what: str) -> subprocess.CompletedProcess:
    r = subprocess.run(args, env={**os.environ, **env_extra}, capture_output=True, text=True)
    if r.returncode != 0:
        raise BackupError(f"{what} failed: {r.stderr.strip()[-600:]}")
    return r


def psql(dsn: str, env: dict[str, str], sql: str) -> str:
    return _run([find_bin("psql"), "-X", "-At", "-v", "ON_ERROR_STOP=1", dsn, "-c", sql], env, "psql").stdout.strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def snapshot_state(dsn: str, env: dict[str, str], deep: bool) -> dict:
    tables = [t for t in psql(dsn, env, "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1").splitlines() if t]
    counts = {t: int(psql(dsn, env, f'SELECT count(*) FROM "{t}"')) for t in tables}
    out = {"tables": counts, "alembic_revision": None}
    if "alembic_version" in counts and counts["alembic_version"]:
        out["alembic_revision"] = psql(dsn, env, "SELECT version_num FROM alembic_version")
    if deep:                    # order-independent content fingerprint per table (expensive on big tables)
        out["fingerprints"] = {t: psql(dsn, env, f'SELECT coalesce(md5(string_agg(x::text, \'\' ORDER BY x::text)), \'\') FROM "{t}" x') for t in tables}
    return out


# ------------------------------------------------------------------------------------------------ backup
def create_backup(dsn_url: str, out_dir: Path, storage_dir: Path | None = None, deep: bool = False) -> dict:
    dsn, env = normalise_dsn(dsn_url)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dump = out_dir / f"nexus-{stamp}.dump"
    t0 = time.monotonic()
    check_client_matches_server(dsn, env, ("pg_dump",))
    state = snapshot_state(dsn, env, deep)
    _run([find_bin("pg_dump"), "--format=custom", "--no-owner", "--no-privileges", "--file", str(dump), dsn], env, "pg_dump")
    manifest = {"format": FORMAT_VERSION, "created_at": stamp, "database": db_name(dsn),
                "server_version": psql(dsn, env, "SHOW server_version"), "dump": dump.name, "dump_sha256": sha256_file(dump), "dump_bytes": dump.stat().st_size,
                "state": state, "backup_seconds": round(time.monotonic() - t0, 2), "storage": None}
    if storage_dir is not None:
        arc = out_dir / f"nexus-{stamp}.storage.tar.gz"
        files = {str(p.relative_to(storage_dir)): sha256_file(p) for p in sorted(storage_dir.rglob("*")) if p.is_file()}
        with tarfile.open(arc, "w:gz") as tf:
            tf.add(storage_dir, arcname="storage")
        manifest["storage"] = {"archive": arc.name, "archive_sha256": sha256_file(arc), "files": files}
    mf = out_dir / f"nexus-{stamp}.manifest.json"
    mf.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    manifest["manifest_path"] = str(mf)
    return manifest


# ------------------------------------------------------------------------------------------------ verify
def load_manifest(manifest_path: Path) -> dict:
    try:
        m = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise BackupError(f"cannot read manifest {manifest_path}: {exc}") from exc
    if m.get("format", FORMAT_VERSION) != FORMAT_VERSION or not {"dump", "dump_sha256", "state"} <= set(m):
        raise BackupError("unsupported or malformed manifest")
    return m


def verify_backup(manifest_path: Path) -> dict:
    m = load_manifest(manifest_path)
    base = manifest_path.parent
    problems = []
    dump = base / m["dump"]
    if not dump.exists():
        problems.append("dump file missing")
    elif sha256_file(dump) != m["dump_sha256"]:
        problems.append("dump checksum mismatch (corrupted or tampered)")
    else:
        try:
            listing = _run([find_bin("pg_restore"), "--list", str(dump)], {}, "pg_restore --list").stdout
            if "TABLE DATA" not in listing:
                problems.append("dump contains no table data")
        except BackupError as exc:
            problems.append(str(exc))
    st = m.get("storage")
    if st:
        arc = base / st["archive"]
        if not arc.exists() or sha256_file(arc) != st["archive_sha256"]:
            problems.append("storage archive missing or checksum mismatch")
    return {"ok": not problems, "problems": problems, "dump": m["dump"], "created_at": m["created_at"]}


# ------------------------------------------------------------------------------------------------ restore drill
def restore_drill(manifest_path: Path, admin_dsn_url: str, keep: bool = False) -> dict:
    v = verify_backup(manifest_path)
    if not v["ok"]:
        raise BackupError("backup failed verification: " + "; ".join(v["problems"]))
    m = load_manifest(manifest_path)
    dump = manifest_path.parent / m["dump"]
    admin, env = normalise_dsn(admin_dsn_url)
    scratch = "nexus_drill_" + secrets.token_hex(4)
    scratch_dsn = with_db(admin, scratch)
    report: dict = {"scratch_database": scratch, "problems": []}
    check_client_matches_server(with_db(admin, "postgres"), env, ("pg_restore",))
    psql(with_db(admin, "postgres"), env, f'CREATE DATABASE "{scratch}"')
    try:
        t0 = time.monotonic()
        _run([find_bin("pg_restore"), "--no-owner", "--no-privileges", "--exit-on-error", "--dbname", scratch_dsn, str(dump)], env, "pg_restore")
        report["restore_seconds"] = round(time.monotonic() - t0, 2)
        got = snapshot_state(scratch_dsn, env, deep="fingerprints" in m["state"])
        want = m["state"]
        for t, n in want["tables"].items():
            if got["tables"].get(t) != n:
                report["problems"].append(f"row count mismatch in {t}: backup {n}, restored {got['tables'].get(t)}")
        for t in set(got["tables"]) - set(want["tables"]):
            report["problems"].append(f"unexpected table after restore: {t}")
        if got["alembic_revision"] != want["alembic_revision"]:
            report["problems"].append(f"alembic revision mismatch: {want['alembic_revision']} vs {got['alembic_revision']}")
        for t, fp in (want.get("fingerprints") or {}).items():
            if got["fingerprints"].get(t) != fp:
                report["problems"].append(f"content fingerprint mismatch in {t}")
        # pgvector must work after a restore: extension present, vector search returns rows, indexes rebuilt
        report["pgvector"] = psql(scratch_dsn, env, "SELECT extversion FROM pg_extension WHERE extname='vector'") or None
        if want["tables"].get("chunks"):
            if not report["pgvector"]:
                report["problems"].append("pgvector extension missing after restore")
            else:
                n = psql(scratch_dsn, env, "SELECT count(*) FROM (SELECT id FROM chunks WHERE embedding IS NOT NULL ORDER BY embedding <=> "
                                           "(SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1) LIMIT 3) s")
                report["vector_search_rows"] = int(n)
                if int(n) == 0 and psql(scratch_dsn, env, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL") != "0":
                    report["problems"].append("vector search returned nothing")
            idx = set(psql(scratch_dsn, env, "SELECT indexname FROM pg_indexes WHERE tablename='chunks'").splitlines())
            report["chunk_indexes"] = sorted(idx)
            for needed in ("ix_chunks_fts", "ix_chunks_embedding_hnsw"):
                if needed not in idx:
                    report["problems"].append(f"index {needed} missing after restore")
    finally:
        if not keep:
            psql(with_db(admin, "postgres"), env, f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
    report["ok"] = not report["problems"]
    report["tables_checked"] = len(m["state"]["tables"])
    report["rows_in_backup"] = sum(m["state"]["tables"].values())
    return report


# ------------------------------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--dsn", default=os.environ.get("NEXUS_DATABASE_URL"))
    b.add_argument("--storage-dir", type=Path)
    b.add_argument("--deep", action="store_true", help="also fingerprint every table's content (slow on large tables)")
    v = sub.add_parser("verify")
    v.add_argument("--manifest", required=True, type=Path)
    d = sub.add_parser("drill")
    d.add_argument("--manifest", required=True, type=Path)
    d.add_argument("--admin-dsn", default=os.environ.get("NEXUS_DATABASE_URL"), help="server to restore into (needs CREATEDB); default: the production URL's server")
    d.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "backup":
            if not a.dsn:
                raise BackupError("--dsn or NEXUS_DATABASE_URL required")
            m = create_backup(a.dsn, a.out, a.storage_dir, a.deep)
            print(json.dumps({k: m[k] for k in ("manifest_path", "dump", "dump_bytes", "dump_sha256", "backup_seconds")}, indent=2))
            return 0
        if a.cmd == "verify":
            r = verify_backup(a.manifest)
        else:
            if not a.admin_dsn:
                raise BackupError("--admin-dsn or NEXUS_DATABASE_URL required")
            r = restore_drill(a.manifest, a.admin_dsn, a.keep)
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
