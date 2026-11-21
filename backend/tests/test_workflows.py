"""Phase 18: the workflow validator, proven by mutation, plus cross-checks that the workflows reference things that really exist in this repo."""
from __future__ import annotations

import copy
import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("validate_workflows", ROOT / "infrastructure" / "scripts" / "validate_workflows.py")
V = importlib.util.module_from_spec(spec)
sys.modules["validate_workflows"] = V
spec.loader.exec_module(V)

GOOD = yaml.safe_load("""
name: t
on: {pull_request: {}}
permissions: {contents: read}
jobs:
  a:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
        with: {persist-credentials: false}
      - run: echo hi
  b:
    needs: [a]
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo ok
""")


def mutate(fn):
    d = copy.deepcopy(GOOD)
    fn(d)
    return V.validate_workflow(d, "t.yml")


def test_good_workflow_is_clean():
    assert V.validate_workflow(GOOD, "t.yml") == ([], [])


@pytest.mark.parametrize("fn,needle", [
    (lambda d: d.pop("permissions"), "permissions` missing"),
    (lambda d: d.update(permissions="write-all"), "read-only"),
    (lambda d: d.update(permissions={"contents": "read", "packages": "write"}), "read-only"),
    (lambda d: d["jobs"]["a"].pop("timeout-minutes"), "timeout-minutes"),
    (lambda d: d.update({"on": {"pull_request_target": {}}}), "pull_request_target"),
    (lambda d: d["jobs"]["b"].update(needs=["zzz"]), "unknown job"),
    (lambda d: d["jobs"]["a"]["steps"].append({"run": "echo ${{ github.event.pull_request.title }}"}), "script injection"),
    (lambda d: d["jobs"]["a"]["steps"].append({"run": "echo ${{ github.head_ref }}"}), "script injection"),
    (lambda d: d["jobs"]["a"]["steps"].append({"run": "echo ${{ inputs.version }}"}), "script injection"),
    (lambda d: d["jobs"]["a"]["steps"].append({"run": "echo ${{ github.ref_name }}"}), "script injection"),
    (lambda d: d["jobs"]["a"]["steps"].append({"run": "echo ${{ steps.x.outputs.y }}"}), "script injection"),
    (lambda d: d["jobs"]["a"]["steps"][0].update(uses="actions/checkout@main"), "not 'main'"),
    (lambda d: d["jobs"]["a"]["steps"][0].update(uses="actions/checkout"), "no version pin"),
    (lambda d: d["jobs"]["a"]["steps"][0].update(uses="actions/checkout@latest"), "not 'latest'"),
    (lambda d: d["jobs"]["a"]["steps"][0].update({"with": {}}), "persist-credentials"),
    (lambda d: d["jobs"]["a"]["steps"].append({"uses": "docker://alpine:3"}), "digest"),
    (lambda d: d["jobs"]["a"]["steps"].append({"run": "kubectl apply -f x.yaml"}), "no `environment:`"),
    (lambda d: d["jobs"]["a"]["steps"].append({"uses": "aws-actions/configure-aws-credentials@v4"}), "no `environment:`"),
    (lambda d: d["jobs"]["a"]["steps"].append({"run": "terraform apply"}), "no `environment:`"),
    (lambda d: d["jobs"]["a"].update(permissions={"id-token": "write"}), "nothing in the job uses OIDC"),
    (lambda d: d["jobs"]["a"].update(permissions="write-all"), "write-all"),
    (lambda d: d["jobs"]["b"].update(secrets="inherit"), "secrets: inherit"),
    (lambda d: d["jobs"]["a"]["steps"].append({"uses": "aquasecurity/trivy-action@0.28.0", "continue-on-error": True}), "continue-on-error"),
    (lambda d: d.update(jobs={}), "no jobs"),
])
def test_validator_catches(fn, needle):
    errs, _ = mutate(fn)
    assert any(needle in e for e in errs), errs


def test_safe_variants_are_allowed():
    def ok(d):
        d["jobs"]["a"]["steps"].append({"run": 'echo "$TAG"', "env": {"TAG": "${{ github.ref_name }}"}})          # via env: fine
        d["jobs"]["a"]["steps"].append({"run": "echo ${{ github.sha }} ${{ runner.os }}"})
        d["jobs"]["a"]["steps"].append({"uses": "actions/setup-python@" + "a" * 40})
        d["jobs"]["a"]["steps"].append({"uses": "./.github/actions/local"})
        d["jobs"]["a"]["steps"].append({"uses": "docker://alpine@sha256:" + "b" * 64})
        d["jobs"]["a"].update(environment="staging", steps=d["jobs"]["a"]["steps"] + [{"run": "kubectl apply -f x"}])
    assert mutate(ok) == ([], [])


def test_third_party_tag_pin_is_a_warning_and_sha_is_clean():
    errs, warns = mutate(lambda d: d["jobs"]["a"]["steps"].append({"uses": "hashicorp/setup-terraform@v3"}))
    assert errs == [] and len(warns) == 1 and "commit SHA" in warns[0]
    assert mutate(lambda d: d["jobs"]["a"]["steps"].append({"uses": "hashicorp/setup-terraform@" + "c" * 40})) == ([], [])


def test_job_level_id_token_with_oidc_user_is_accepted():
    def ok(d):
        d["jobs"]["a"].update(environment="prod", permissions={"contents": "read", "id-token": "write"})
        d["jobs"]["a"]["steps"].append({"uses": "aws-actions/configure-aws-credentials@v4"})
    errs, _ = mutate(ok)
    assert errs == []


def test_bare_on_key_parsed_as_true_is_handled():
    d = copy.deepcopy(GOOD)
    assert True in d and "on" not in d              # PyYAML turns the bare `on:` key into boolean True
    d[True] = {"pull_request_target": {}}
    assert any("pull_request_target" in e for e in V.validate_workflow(d, "t")[0])


def test_repository_workflows_pass_and_warnings_are_only_third_party_pins():
    errs, warns = V.validate_dir()
    assert errs == []
    assert warns and all("third-party action pinned by tag" in w for w in warns)
    assert {p.name for p in (ROOT / ".github/workflows").glob("*.yml")} >= {"ci.yml", "release.yml", "deploy.yml", "terraform.yml"}


def test_invalid_yaml_and_empty_dir(tmp_path):
    (tmp_path / "x.yml").write_text("a: [unclosed")
    assert "invalid YAML" in V.validate_dir(tmp_path)[0][0]
    assert V.validate_dir(tmp_path / "none") == (["no workflows found"], [])


# ---- cross-checks: the workflows must refer to real things ---------------------------------------------------------------------------------
def _wf(name):
    return (ROOT / ".github/workflows" / name).read_text()


def test_deploy_workflow_names_match_the_kubernetes_manifests():
    base = {}
    for f in (ROOT / "deploy/k8s/base").glob("*.yaml"):
        for doc in yaml.safe_load_all(f.read_text()):
            if doc and doc.get("kind") == "Deployment":
                base[doc["metadata"]["name"]] = doc
    assert set(base) == {"api", "worker", "frontend"}
    wf = _wf("deploy.yml")
    assert set(re.findall(r"for d in ([a-z ]+);", wf)) == {"api worker frontend"}
    assert "nexus-api" not in wf
    ns = yaml.safe_load((ROOT / "deploy/k8s/base/kustomization.yaml").read_text()).get("namespace")
    assert ns == "nexus" and "-n nexus" in wf
    # the migrate Job the workflow waits for is the one that exists
    job = yaml.safe_load((ROOT / "deploy/k8s/migrate/job.yaml").read_text())
    assert job["metadata"]["name"] == "nexus-migrate" and "job/nexus-migrate" in wf
    # images the wrapper overlay re-points are the images the manifests use
    for img in ("registry.example.com/nexus/api", "registry.example.com/nexus/frontend"):
        assert img in wf and any(img in c["image"] for d in base.values() for c in d["spec"]["template"]["spec"]["containers"])


def test_workflow_script_and_dockerfile_references_exist():
    for wf in ("ci.yml", "release.yml", "deploy.yml", "terraform.yml"):
        for p in re.findall(r"(infrastructure/scripts/[\w./-]+\.py)", _wf(wf)):
            assert (ROOT / p).exists() or p.endswith("validate_terraform.py"), f"{wf} references missing {p}"
    for p in ("backend/Dockerfile", "frontend/Dockerfile.unprivileged", "backend/requirements-dev.txt", "frontend/package-lock.json"):
        assert (ROOT / p).exists()
    rel = _wf("release.yml")
    assert "backend/Dockerfile" in rel and "frontend/Dockerfile.unprivileged" in rel and "tag-matches" in rel and 'notes "${TAG#v}"' in rel


def test_smoke_test_endpoints_exist_in_the_openapi_schema_and_proxy_path():
    paths = __import__("json").loads((ROOT / "docs/api/openapi.json").read_text())["paths"]
    assert "/health/version" in paths and "/health/ready" in paths
    assert "/api/health/version" in _wf("deploy.yml") and "location /api/" in (ROOT / "frontend/nginx.unprivileged.conf").read_text()


def test_release_and_deploy_are_gated_correctly():
    rel, dep, tf = (yaml.safe_load(_wf(n)) for n in ("release.yml", "deploy.yml", "terraform.yml"))
    assert (rel.get("on") or rel[True])["push"]["tags"]                         # tag-triggered only
    assert "pull_request" not in (rel.get("on") or rel[True])
    d = dep["jobs"]["deploy"]
    assert d["environment"] and d["permissions"]["id-token"] == "write"
    assert "workflow_dispatch" in (dep.get("on") or dep[True]) and "push" not in (dep.get("on") or dep[True])
    assert tf["jobs"]["plan-apply"]["if"] == "github.event_name == 'workflow_dispatch'" and tf["jobs"]["plan-apply"]["environment"]
    # apply only ever runs a saved plan, and only when asked
    assert "terraform apply -input=false tfplan" in _wf("terraform.yml") and "${{ inputs.apply }}" in _wf("terraform.yml")
    # image signing happens by digest, and the deploy verifies the signer identity
    assert 'cosign sign --yes "$REF"' in _wf("release.yml") and "--certificate-identity-regexp" in _wf("deploy.yml")
