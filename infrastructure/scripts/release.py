#!/usr/bin/env python3
"""Release engineering checks and version bumping.

  release.py check [--skip-openapi]   everything that must be true before tagging (exit 1 on failure)
  release.py bump X.Y.Z               update every version location + roll the CHANGELOG ([Unreleased] -> [X.Y.Z])
  release.py lint-migrations          expand/contract safety lint only
  release.py notes X.Y.Z              print the CHANGELOG section for a version (used for GitHub Release notes)
  release.py tag-matches TAG          exit 0 only if TAG is exactly v<current version>

Why the migration lint: the API and worker are rolled out gradually, so for a while *old code runs against the new schema* (and, on rollback, new schema against old code).
A migration that drops/renames/tightens something old code still uses breaks that window. `upgrade()` must therefore be additive ("expand"); destructive steps ("contract") belong in a later
release once nothing reads the old shape, and must be acknowledged with the comment `# release: destructive-approved` on or directly above the statement."""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$")
APPROVED = "release: destructive-approved"
DESTRUCTIVE_SQL = re.compile(r"\b(DROP\s+(TABLE|COLUMN|INDEX|CONSTRAINT|SCHEMA|TYPE)|TRUNCATE|DELETE\s+FROM|RENAME\s+(TO|COLUMN)|ALTER\s+COLUMN\s+\w+\s+(SET\s+NOT\s+NULL|TYPE))\b", re.I)


# ------------------------------------------------------------------------------------------------ migration lint
def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_false(n) -> bool:
    return isinstance(n, ast.Constant) and n.value is False


def lint_migration_source(src: str, filename: str = "<migration>") -> list[str]:
    tree = ast.parse(src)
    lines = src.splitlines()
    problems: list[str] = []
    up = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None)
    if up is None:
        return [f"{filename}: no upgrade() function"]

    def approved(lineno: int) -> bool:
        return any(APPROVED in lines[i] for i in (lineno - 1, lineno - 2) if 0 <= i < len(lines))

    def flag(node: ast.AST, msg: str) -> None:
        if not approved(node.lineno):
            problems.append(f"{filename}:{node.lineno}: {msg}")

    for node in ast.walk(up):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        name = node.func.attr
        owner = node.func.value
        if not (isinstance(owner, ast.Name) and owner.id == "op"):
            continue
        if name in ("drop_table", "drop_column", "drop_constraint", "rename_table"):
            flag(node, f"op.{name}() in upgrade(): old code may still use it (contract step needs approval)")
        elif name == "alter_column":
            if _kw(node, "new_column_name") is not None:
                flag(node, "column rename in upgrade(): add a new column and migrate instead")
            if _kw(node, "type_") is not None:
                flag(node, "column type change in upgrade(): can rewrite the table / break old code")
            if _is_false(_kw(node, "nullable")) and _kw(node, "server_default") is None:
                flag(node, "making a column NOT NULL in upgrade(): old code may still write NULL")
        elif name == "add_column":
            col = node.args[1] if len(node.args) > 1 else None
            if isinstance(col, ast.Call) and _is_false(_kw(col, "nullable")) and _kw(col, "server_default") is None and _kw(col, "default") is None:
                flag(node, "NOT NULL column without server_default: fails on populated tables and breaks old code's inserts")
        elif name == "execute":
            arg = node.args[0] if node.args else None
            text = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
            if text is None:
                flag(node, "op.execute() with non-literal SQL cannot be linted")
            elif DESTRUCTIVE_SQL.search(text):
                flag(node, f"destructive SQL in upgrade(): {text[:60]!r}")
    return problems


def lint_migrations(dirpath: Path | None = None) -> list[str]:
    out: list[str] = []
    for f in sorted((dirpath or ROOT / "backend" / "migrations" / "versions").glob("*.py")):
        out += lint_migration_source(f.read_text(), f.name)
    return out


def alembic_heads(dirpath: Path | None = None) -> list[str]:
    revs: dict[str, str | None] = {}
    for f in (dirpath or ROOT / "backend" / "migrations" / "versions").glob("*.py"):
        t = ast.parse(f.read_text())
        vals = {}
        for n in t.body:
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in ("revision", "down_revision") and isinstance(n.value, ast.Constant):
                vals[n.targets[0].id] = n.value.value
        if "revision" in vals:
            revs[vals["revision"]] = vals.get("down_revision")
    parents = {d for d in revs.values() if d}
    return sorted(r for r in revs if r not in parents)


# ------------------------------------------------------------------------------------------------ versions
def read_versions() -> dict[str, str | None]:
    v: dict[str, str | None] = {}
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "backend/app/version.py").read_text())
    v["backend/app/version.py"] = m.group(1) if m else None
    v["frontend/package.json"] = json.loads((ROOT / "frontend/package.json").read_text()).get("version")
    for f in sorted([*(ROOT / "deploy/k8s/overlays").glob("*/kustomization.yaml"), *(ROOT / "deploy/k8s/migrate").glob("kustomization.yaml")]):
        tags = set(re.findall(r'newTag:\s*"?([^"\s#]+)"?', f.read_text()))
        v[str(f.relative_to(ROOT))] = tags.pop() if len(tags) == 1 else ("mixed:" + ",".join(sorted(tags)) if tags else None)
    return v


def changelog_has(version: str, text: str | None = None) -> bool:
    text = text if text is not None else (ROOT / "CHANGELOG.md").read_text()
    return re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$", text, re.M) is not None


def changelog_section(version: str, text: str) -> str:
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def openapi_fresh() -> bool:
    code = ("import json,sys;sys.path.insert(0,'backend');from app.core.config import Settings;from app.main import create_app;"
            "a=create_app(Settings(env='test',database_url='sqlite+aiosqlite:///:memory:',log_json=False));print(json.dumps(a.openapi(),indent=2,sort_keys=True))")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-400:])
    return r.stdout.strip() == (ROOT / "docs/api/openapi.json").read_text().strip()


def check(skip_openapi: bool = False) -> list[str]:
    errs: list[str] = []
    vs = read_versions()
    main = vs["backend/app/version.py"]
    if not main or not SEMVER.match(main):
        errs.append(f"backend/app/version.py: {main!r} is not SemVer")
    for where, ver in vs.items():
        if ver != main:
            errs.append(f"version drift: {where} has {ver!r}, backend/app/version.py has {main!r}")
    if main and not changelog_has(main):
        errs.append(f"CHANGELOG.md has no dated '## [{main}] - YYYY-MM-DD' section")
    if "## [Unreleased]" not in (ROOT / "CHANGELOG.md").read_text():
        errs.append("CHANGELOG.md lacks an [Unreleased] section")
    heads = alembic_heads()
    if len(heads) != 1:
        errs.append(f"alembic must have exactly one head, found {heads}")
    errs += lint_migrations()
    if not skip_openapi:
        try:
            if not openapi_fresh():
                errs.append("docs/api/openapi.json is stale: run python infrastructure/scripts/export_openapi.py")
        except RuntimeError as exc:
            errs.append(f"could not generate OpenAPI: {exc}")
    return errs


def bump(new: str) -> None:
    if not SEMVER.match(new):
        raise SystemExit(f"not SemVer: {new}")
    cur = read_versions()["backend/app/version.py"]
    if new == cur:
        raise SystemExit(f"already at {new}")
    cl = (ROOT / "CHANGELOG.md").read_text()
    if "## [Unreleased]" not in cl:
        raise SystemExit("CHANGELOG.md has no [Unreleased] section")
    if not changelog_section("Unreleased", cl):
        raise SystemExit("[Unreleased] is empty: write the release notes first")
    from datetime import date

    p = ROOT / "backend/app/version.py"
    p.write_text(re.sub(r'(__version__\s*=\s*")[^"]+(")', rf"\g<1>{new}\g<2>", p.read_text()))
    p = ROOT / "frontend/package.json"
    p.write_text(re.sub(r'("version":\s*")[^"]+(")', rf"\g<1>{new}\g<2>", p.read_text(), count=1))
    for f in [*(ROOT / "deploy/k8s/overlays").glob("*/kustomization.yaml"), *(ROOT / "deploy/k8s/migrate").glob("kustomization.yaml")]:
        f.write_text(re.sub(r'(newTag:\s*")[^"]+(")', rf"\g<1>{new}\g<2>", f.read_text()))
    (ROOT / "CHANGELOG.md").write_text(cl.replace("## [Unreleased]", f"## [Unreleased]\n\n## [{new}] - {date.today().isoformat()}", 1))
    print(f"bumped {cur} -> {new}. Next: regenerate OpenAPI, run `release.py check`, commit, tag v{new}.")


def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if a and a[0] == "notes" and len(a) == 2:
        body = changelog_section(a[1], (ROOT / "CHANGELOG.md").read_text())
        if not body:
            print(f"no CHANGELOG section for {a[1]}", file=sys.stderr)
            return 1
        print(body)
        return 0
    if a and a[0] == "tag-matches" and len(a) == 2:
        want = "v" + str(read_versions()["backend/app/version.py"])
        if a[1] != want:
            print(f"tag {a[1]!r} does not match the code version (expected {want!r})", file=sys.stderr)
            return 1
        return 0
    if not a or a[0] not in ("check", "bump", "lint-migrations"):
        print(__doc__)
        return 2
    if a[0] == "bump":
        if len(a) != 2:
            print("usage: release.py bump X.Y.Z")
            return 2
        bump(a[1])
        return 0
    errs = lint_migrations() if a[0] == "lint-migrations" else check("--skip-openapi" in a)
    for e in errs:
        print("FAIL:", e)
    print("release checks: " + ("FAILED" if errs else "ok"))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
