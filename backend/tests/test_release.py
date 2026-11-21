"""Phase 18: release tooling. The migration lint is proven by mutation (each unsafe pattern must be caught, each safe one allowed)."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("release_tool", ROOT / "infrastructure" / "scripts" / "release.py")
R = importlib.util.module_from_spec(spec)
sys.modules["release_tool"] = R
spec.loader.exec_module(R)


def lint(body: str) -> list[str]:
    return R.lint_migration_source("from alembic import op\nimport sqlalchemy as sa\nrevision='9'\ndown_revision=None\ndef upgrade():\n" + "".join("    " + l + "\n" for l in body.splitlines()))


@pytest.mark.parametrize("stmt,needle", [
    ("op.drop_table('x')", "drop_table"),
    ("op.drop_column('t', 'c')", "drop_column"),
    ("op.drop_constraint('c', 't')", "drop_constraint"),
    ("op.rename_table('a', 'b')", "rename_table"),
    ("op.alter_column('t', 'c', new_column_name='d')", "rename"),
    ("op.alter_column('t', 'c', type_=sa.Text())", "type change"),
    ("op.alter_column('t', 'c', nullable=False)", "NOT NULL"),
    ("op.add_column('t', sa.Column('c', sa.Text(), nullable=False))", "without server_default"),
    ("op.execute('DROP TABLE t')", "destructive SQL"),
    ("op.execute('delete from t')", "destructive SQL"),
    ("op.execute('ALTER TABLE t RENAME COLUMN a TO b')", "destructive SQL"),
    ("op.execute('ALTER TABLE t ALTER COLUMN a SET NOT NULL')", "destructive SQL"),
    ("q = 'x'\nop.execute(q)", "non-literal"),
])
def test_lint_flags_unsafe_patterns(stmt, needle):
    out = lint(stmt)
    assert out and needle in out[0], out


@pytest.mark.parametrize("stmt", [
    "op.create_table('x', sa.Column('id', sa.String(), nullable=False))",
    "op.add_column('t', sa.Column('c', sa.Text(), nullable=True))",
    "op.add_column('t', sa.Column('c', sa.Text(), nullable=False, server_default='x'))",
    "op.create_index('i', 't', ['c'])",
    "op.alter_column('t', 'c', nullable=False, server_default='x')",
    "op.execute('CREATE INDEX IF NOT EXISTS i ON t (c)')",
])
def test_lint_allows_additive_changes(stmt):
    assert lint(stmt) == []


def test_downgrade_may_be_destructive_and_approval_marker_works():
    src = "from alembic import op\ndef upgrade():\n    pass\ndef downgrade():\n    op.drop_table('x')\n"
    assert R.lint_migration_source(src) == []
    assert lint("# release: destructive-approved (contract step, nothing reads x since 0.4.0)\nop.drop_table('x')") == []
    assert lint("op.drop_table('x')  # release: destructive-approved") == []
    assert lint("# release: destructive-approved\nop.execute('select 1')\nop.drop_table('x')")        # approval covers only the next line
    assert R.lint_migration_source("x = 1") == ["<migration>: no upgrade() function"]


def test_nested_statements_are_found():
    assert lint("if True:\n    for t in ['a']:\n        op.drop_table(t)")


def test_all_shipped_migrations_pass_and_have_one_head():
    assert R.lint_migrations() == []
    assert R.alembic_heads() == ["0007"]


def test_multiple_heads_are_detected(tmp_path):
    (tmp_path / "a.py").write_text("revision='1'\ndown_revision=None\n")
    (tmp_path / "b.py").write_text("revision='2'\ndown_revision='1'\n")
    (tmp_path / "c.py").write_text("revision='3'\ndown_revision='1'\n")
    assert R.alembic_heads(tmp_path) == ["2", "3"]


def test_repo_passes_release_check():
    assert R.check(skip_openapi=False) == []


def test_changelog_parsing():
    t = "## [Unreleased]\n\n## [1.2.3] - 2026-01-02\n### Added\n- a\n\n## [1.2.2] - 2026-01-01\n- b\n"
    assert R.changelog_has("1.2.3", t) and not R.changelog_has("1.2.4", t) and not R.changelog_has("1.2", t)
    assert R.changelog_section("1.2.3", t) == "### Added\n- a"
    assert R.changelog_section("9.9.9", t) == ""


def _clone(tmp_path):
    for rel in ("backend/app/version.py", "frontend/package.json", "deploy/k8s/overlays/example/kustomization.yaml", "deploy/k8s/migrate/kustomization.yaml", "CHANGELOG.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    return tmp_path


def test_bump_updates_every_location_and_check_catches_drift(tmp_path, monkeypatch):
    root = _clone(tmp_path)
    monkeypatch.setattr(R, "ROOT", root)
    cl = (root / "CHANGELOG.md").read_text().replace("## [Unreleased]\n", "## [Unreleased]\n### Fixed\n- something\n", 1)
    (root / "CHANGELOG.md").write_text(cl)
    R.bump("0.2.0")
    assert set(R.read_versions().values()) == {"0.2.0"}
    assert R.changelog_has("0.2.0") and R.changelog_has("0.1.0")
    assert "- something" in R.changelog_section("0.2.0", (root / "CHANGELOG.md").read_text())
    assert [e for e in R.check(skip_openapi=True) if "drift" in e or "CHANGELOG" in e] == []
    # drift: someone edits only the overlay
    ov = root / "deploy/k8s/overlays/example/kustomization.yaml"
    ov.write_text(ov.read_text().replace('newTag: "0.2.0"', 'newTag: "0.1.9"', 1))
    errs = R.check(skip_openapi=True)
    assert any("version drift" in e and "kustomization" in e for e in errs)
    assert json.loads((root / "frontend/package.json").read_text())["version"] == "0.2.0"


def test_bump_refuses_bad_input(tmp_path, monkeypatch):
    root = _clone(tmp_path)
    monkeypatch.setattr(R, "ROOT", root)
    for bad in ("1.0", "v1.0.0", "1.0.0.0", "latest"):
        with pytest.raises(SystemExit, match="SemVer"):
            R.bump(bad)
    with pytest.raises(SystemExit, match="already at"):
        R.bump("0.1.0")
    with pytest.raises(SystemExit, match="empty"):          # nothing documented under [Unreleased]
        R.bump("0.2.0")


def test_check_flags_missing_changelog_and_non_semver(tmp_path, monkeypatch):
    root = _clone(tmp_path)
    monkeypatch.setattr(R, "ROOT", root)
    (root / "backend/app/version.py").write_text('__version__ = "1.0"\n')
    errs = R.check(skip_openapi=True)
    assert any("not SemVer" in e for e in errs) and any("version drift" in e for e in errs) and any("no dated" in e for e in errs)


async def test_version_endpoint(tmp_path):
    import httpx
    from app.core.config import Settings
    from app.main import create_app
    from app.version import __version__

    a = create_app(Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", storage_dir=str(tmp_path / "s"), log_json=False, embedding_dim=64))
    async with a.router.lifespan_context(a):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://t") as c:
            r = await c.get("/health/version")
    assert r.status_code == 200 and r.json() == {"version": __version__} and a.version == __version__


def test_migrate_job_tag_is_tracked_and_notes_and_tag_matching(tmp_path, monkeypatch, capsys):
    assert "deploy/k8s/migrate/kustomization.yaml" in R.read_versions()          # the migrate Job image must move with the release
    assert R.main(["notes", "0.1.0"]) == 0 and "Phases 1" in capsys.readouterr().out
    assert R.main(["notes", "9.9.9"]) == 1
    assert R.main(["tag-matches", "v0.1.0"]) == 0
    assert R.main(["tag-matches", "0.1.0"]) == 1 and R.main(["tag-matches", "v0.1.1"]) == 1
