#!/usr/bin/env python3
"""Static policy checks for .github/workflows/*.yml (no GitHub access needed). Exit 1 on errors; warnings are printed (and fail with --strict).

Errors: missing/over-broad top-level `permissions`; job without `timeout-minutes`; `pull_request_target`; untrusted context interpolated into `run:` (script injection; pass it via `env:`);
`needs` pointing at a missing job; deploy-like jobs without a GitHub `environment`; `id-token: write` / `packages: write` / `contents: write` anywhere but at job level; actions that are not pinned
to a version tag or SHA (branches, `latest`); `actions/checkout` without `persist-credentials: false`; `docker://` images without a digest; `secrets: inherit`.
Warnings: third-party (non-actions/github) actions pinned by tag rather than commit SHA. Tags are mutable, so before go-live run `pinact`/Dependabot to rewrite them to SHAs."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FIRST_PARTY = {"actions", "github"}
UNTRUSTED = re.compile(r"\$\{\{\s*(github\.event\b|github\.head_ref|github\.ref_name|github\.ref\b|github\.base_ref|inputs\.|needs\.[\w-]+\.outputs|steps\.[\w-]+\.outputs)", re.I)
SHA = re.compile(r"^[0-9a-f]{40}$")
VERSION_TAG = re.compile(r"^v?\d+(\.\d+){0,2}([-.][0-9A-Za-z.]+)?$")
DEPLOY_HINT = re.compile(r"kubectl\s+(apply|rollout|delete|set)|aws\s+eks\s+update-kubeconfig|terraform\s+apply|helm\s+(upgrade|install)|configure-aws-credentials", re.I)
WRITE_SCOPES = {"id-token", "packages", "contents", "deployments", "actions", "security-events", "pull-requests", "issues"}


def _perm_writes(perms) -> set[str]:
    if perms == "write-all":
        return {"*"}
    if isinstance(perms, dict):
        return {k for k, v in perms.items() if v == "write"}
    return set()


def validate_workflow(doc: dict, name: str) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict) or not doc["jobs"]:
        return [f"{name}: no jobs"], warns
    triggers = doc.get("on", doc.get(True))           # PyYAML parses a bare `on:` key as boolean True
    names = set(triggers) if isinstance(triggers, (dict, list)) else {triggers}
    if "pull_request_target" in names:
        errs.append(f"{name}: pull_request_target runs untrusted PR code with secrets; use pull_request")
    if "permissions" not in doc:
        errs.append(f"{name}: top-level `permissions` missing (the default token is too broad)")
    elif _perm_writes(doc["permissions"]):
        errs.append(f"{name}: top-level permissions must be read-only; grant writes per job ({sorted(_perm_writes(doc['permissions']))})")
    for jid, job in doc["jobs"].items():
        where = f"{name}:{jid}"
        if not isinstance(job, dict):
            errs.append(f"{where}: malformed job")
            continue
        if "timeout-minutes" not in job:
            errs.append(f"{where}: timeout-minutes missing (default is 6 hours)")
        for dep in ([job["needs"]] if isinstance(job.get("needs"), str) else job.get("needs", [])):
            if dep not in doc["jobs"]:
                errs.append(f"{where}: needs unknown job {dep!r}")
        if job.get("secrets") == "inherit":
            errs.append(f"{where}: `secrets: inherit` hands every secret to the callee")
        if "permissions" in job and "*" in _perm_writes(job["permissions"]):
            errs.append(f"{where}: write-all permissions")
        steps = job.get("steps") or []
        uses_refs = [job["uses"]] if "uses" in job else []
        deployish = False
        for i, st in enumerate(steps):
            label = f"{where}.step{i + 1}"
            run = st.get("run")
            if isinstance(run, str):
                if UNTRUSTED.search(run):
                    errs.append(f"{label}: untrusted context interpolated into `run:` (script injection); pass it through `env:`")
                if DEPLOY_HINT.search(run):
                    deployish = True
            if "uses" in st:
                uses_refs.append(st["uses"])
                if re.match(r"^actions/checkout@", st["uses"]) and (st.get("with") or {}).get("persist-credentials") is not False:
                    errs.append(f"{label}: actions/checkout must set persist-credentials: false")
                if "configure-aws-credentials" in st["uses"]:
                    deployish = True
            if "continue-on-error" in st and st["continue-on-error"] is True and re.search(r"trivy|scan|cosign|verify", yaml.safe_dump(st), re.I):
                errs.append(f"{label}: security step must not use continue-on-error")
        for u in uses_refs:
            e, w = _check_uses(u)
            errs += [f"{where}: {x}" for x in e]
            warns += [f"{where}: {x}" for x in w]
        if deployish and not job.get("environment"):
            errs.append(f"{where}: touches a cluster/cloud account but has no `environment:` (needed for approvals and scoped variables)")
        w = _perm_writes(job.get("permissions"))
        if ("id-token" in w) and not any("configure-aws-credentials" in u or "cosign" in u for u in uses_refs) and not any("cosign" in (s.get("run") or "") for s in steps):
            errs.append(f"{where}: id-token: write granted but nothing in the job uses OIDC")
    return errs, warns


def _check_uses(u: str) -> tuple[list[str], list[str]]:
    if u.startswith("./"):
        return [], []
    if u.startswith("docker://"):
        return ([] if "@sha256:" in u else [f"{u}: docker image must be pinned by digest"]), []
    if "@" not in u:
        return [f"{u}: action has no version pin"], []
    repo, ref = u.rsplit("@", 1)
    owner = repo.split("/")[0]
    if SHA.match(ref):
        return [], []
    if ref in ("main", "master", "HEAD", "latest", "develop") or not VERSION_TAG.match(ref):
        return [f"{u}: pin to a release tag or commit SHA, not {ref!r}"], []
    if owner not in FIRST_PARTY:
        return [], [f"{u}: third-party action pinned by tag (mutable); pin to a commit SHA before go-live"]
    return [], []


def validate_dir(d: Path | None = None) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    files = sorted((d or ROOT / ".github" / "workflows").glob("*.y*ml"))
    if not files:
        return ["no workflows found"], []
    for f in files:
        try:
            doc = yaml.safe_load(f.read_text())
        except yaml.YAMLError as exc:
            errs.append(f"{f.name}: invalid YAML: {exc}")
            continue
        e, w = validate_workflow(doc, f.name)
        errs += e
        warns += w
    return errs, warns


def main() -> int:
    errs, warns = validate_dir()
    for w in warns:
        print("WARN:", w)
    for e in errs:
        print("FAIL:", e)
    bad = bool(errs) or (bool(warns) and "--strict" in sys.argv)
    print("workflows: " + ("FAILED" if bad else f"ok ({len(warns)} warnings)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
