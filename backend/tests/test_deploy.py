"""Phase 16/18: static validation of deployment artefacts (Kubernetes manifests). No cluster, kubectl or kustomize is available in CI here, so these tests
check wiring and hardening rules statically and prove the validator catches regressions by mutating known-good manifests."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import os
import time
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "deploy" / "k8s"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_k8s", ROOT / "infrastructure" / "scripts" / "validate_k8s.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


V = _load_validator()
SETTINGS_ENV = {"NEXUS_" + name.upper() for name in Settings.model_fields}
PASSTHROUGH = {"NEXUS_RUN_MIGRATIONS"}                                     # read by docker-entrypoint.sh, not by Settings
SECRETS = {"nexus-secrets"}


def probe_paths() -> set[str]:
    app = create_app(Settings(env="test"))
    return set(app.openapi()["paths"])


def docs():
    d, errs = V.load_dir(K8S / "base")
    assert errs == []
    m, merrs = V.load_dir(K8S / "migrate")
    assert merrs == []
    return d + m


def check(ds):
    return V.validate(ds, secret_names=SECRETS, known_env=SETTINGS_ENV, passthrough_env=PASSTHROUGH, probe_paths=probe_paths())


def test_shipped_manifests_are_valid():
    assert check(docs()) == []
    assert V.validate_secret_example(K8S / "base" / "secret.example.yaml") == []


def test_cli_exits_zero_on_the_shipped_tree():
    assert V.main(["validate_k8s.py", str(K8S)]) == 0


def get(ds, kind, name):
    return next(d for d in ds if d["kind"] == kind and d["metadata"]["name"] == name)


def mutate(fn):
    ds = copy.deepcopy(docs())
    fn(ds)
    return check(ds)


MUTATIONS = {
    "latest tag": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"]["containers"][0].update(image="registry.example.com/nexus/api:latest"), "pinned tag"),
    "no tag": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"]["containers"][0].update(image="registry.example.com/nexus/api"), "pinned tag"),
    "root fs writable": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"]["containers"][0]["securityContext"].update(readOnlyRootFilesystem=False), "readOnlyRootFilesystem"),
    "priv esc": (lambda ds: get(ds, "Deployment", "worker")["spec"]["template"]["spec"]["containers"][0]["securityContext"].update(allowPrivilegeEscalation=True), "allowPrivilegeEscalation"),
    "caps kept": (lambda ds: get(ds, "Deployment", "worker")["spec"]["template"]["spec"]["containers"][0]["securityContext"].update(capabilities={"drop": []}), "drop ALL"),
    "no limits": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"]["containers"][0]["resources"].pop("limits"), "resources.limits"),
    "no readiness": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"]["containers"][0].pop("readinessProbe"), "readinessProbe"),
    "no liveness": (lambda ds: get(ds, "Deployment", "worker")["spec"]["template"]["spec"]["containers"][0].pop("livenessProbe"), "livenessProbe"),
    "bad probe path": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"].update(path="/health/nope"), "not a route"),
    "host network": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"].update(hostNetwork=True), "hostNetwork"),
    "sa token": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"].update(automountServiceAccountToken=True), "automountServiceAccountToken"),
    "selector drift": (lambda ds: get(ds, "Deployment", "api")["spec"]["selector"]["matchLabels"].update(app="x"), "selector.matchLabels"),
    "missing configmap": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"]["containers"][0]["envFrom"][0]["configMapRef"].update(name="nope"), "ConfigMap nope"),
    "missing secret": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"]["containers"][0]["envFrom"][1]["secretRef"].update(name="nope"), "Secret nope"),
    "typo env": (lambda ds: get(ds, "ConfigMap", "nexus-config")["data"].update(NEXUS_JWT_SECERT="x"), "NEXUS_JWT_SECERT"),
    "service selector": (lambda ds: get(ds, "Service", "api")["spec"]["selector"].update(app="ghost"), "matches no workload"),
    "service port": (lambda ds: get(ds, "Service", "api")["spec"]["ports"][0].update(targetPort="metrics"), "targetPort metrics"),
    "ingress backend": (lambda ds: get(ds, "Ingress", "nexus")["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"].update(name="ghost"), "ghost"),
    "ingress no tls": (lambda ds: get(ds, "Ingress", "nexus")["spec"].pop("tls"), "no TLS"),
    "hpa target": (lambda ds: get(ds, "HorizontalPodAutoscaler", "api")["spec"]["scaleTargetRef"].update(name="ghost"), "ghost"),
    "hpa min>max": (lambda ds: get(ds, "HorizontalPodAutoscaler", "api")["spec"].update(minReplicas=20), "minReplicas"),
    "hpa no cpu request": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"].pop("cpu"), "requests.cpu"),
    "pdb selector": (lambda ds: get(ds, "PodDisruptionBudget", "api")["spec"]["selector"]["matchLabels"].update(app="ghost"), "matches no workload"),
    "pdb single replica": (lambda ds: (get(ds, "Deployment", "frontend")["spec"].update(replicas=1), get(ds, "HorizontalPodAutoscaler", "api")), "blocks node drains"),
    "volume mount": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"].update(volumes=[]), "volumeMounts without volumes"),
    "no default deny": (lambda ds: ds.remove(get(ds, "NetworkPolicy", "default-deny-ingress")), "default-deny"),
    "no seccomp": (lambda ds: get(ds, "Deployment", "api")["spec"]["template"]["spec"]["securityContext"].pop("seccompProfile"), "seccompProfile"),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_validator_catches_regressions(name):
    fn, needle = MUTATIONS[name]
    errs = mutate(fn)
    assert any(needle in e for e in errs), (name, errs)


def test_orphan_manifest_and_committed_secret_are_detected(tmp_path):
    import shutil
    d = tmp_path / "base"
    shutil.copytree(K8S / "base", d)
    (d / "forgotten.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n")
    _, errs = V.load_dir(d)
    assert any("forgotten.yaml is not listed" in e for e in errs)
    k = yaml.safe_load((d / "kustomization.yaml").read_text())
    k["resources"].append("secret.example.yaml")
    (d / "kustomization.yaml").write_text(yaml.safe_dump(k))
    _, errs = V.load_dir(d)
    assert any("deploys a Secret" in e for e in errs)
    s = yaml.safe_load((d / "secret.example.yaml").read_text())
    s["stringData"]["NEXUS_JWT_SECRET"] = "a-real-looking-secret-value-123456789"
    (d / "secret.example.yaml").write_text(yaml.safe_dump(s))
    assert any("placeholder" in e for e in V.validate_secret_example(d / "secret.example.yaml"))


def test_unpinned_kustomize_image_override_is_detected(tmp_path):
    import shutil
    d = tmp_path / "base"
    shutil.copytree(K8S / "base", d)
    k = yaml.safe_load((d / "kustomization.yaml").read_text())
    k["images"][0]["newTag"] = "latest"
    (d / "kustomization.yaml").write_text(yaml.safe_dump(k))
    assert any("pin a real tag" in e for e in V.load_dir(d)[1])


def test_kubernetes_config_matches_docker_compose_and_the_images():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    compose_keys = {k for k in compose["x-api-env"] if k.startswith("NEXUS_")}
    cm = yaml.safe_load((K8S / "base" / "configmap.yaml").read_text())["data"]
    sec = yaml.safe_load((K8S / "base" / "secret.example.yaml").read_text())["stringData"]
    provided = set(cm) | set(sec)
    # keys compose sets that k8s deliberately omits: the compose-internal service endpoints and per-env LLM/embedding/OTEL knobs have defaults
    optional = {"NEXUS_EMBEDDING_API_KEY", "NEXUS_OTEL_ENABLED", "NEXUS_OTEL_ENDPOINT"}
    assert compose_keys - provided - optional == set(), compose_keys - provided - optional
    # frontend: the manifest's port is the one the unprivileged nginx config actually listens on, and it proxies to the Service name `api`
    nginx = (ROOT / "frontend" / "nginx.unprivileged.conf").read_text()
    port = get(docs(), "Deployment", "frontend")["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"]
    assert f"listen {port};" in nginx and "proxy_pass http://api:8000/" in nginx
    api_svc = get(docs(), "Service", "api")["spec"]["ports"][0]["port"]
    assert api_svc == 8000


async def test_worker_writes_heartbeat_and_liveness_command_reads_it(tmp_path):
    from app.worker import PollingWorker
    hb = tmp_path / "hb"
    s = Settings(env="test", worker_heartbeat_path=str(hb), worker_heartbeat_seconds=0.05)
    w = PollingWorker(None, s)
    t = asyncio.create_task(w._heartbeat())
    await asyncio.sleep(0.2)
    t.cancel()
    assert hb.exists() and time.time() - os.path.getmtime(hb) < 5
    # the exact liveness command from worker.yaml passes on a fresh file and fails on a stale one
    cmd = get(docs(), "Deployment", "worker")["spec"]["template"]["spec"]["containers"][0]["livenessProbe"]["exec"]["command"]
    assert cmd[:2] == ["python", "-c"] and "/tmp/nexus-worker-heartbeat" in cmd[2] and s.model_fields["worker_heartbeat_path"].default == "/tmp/nexus-worker-heartbeat"
    import subprocess
    import sys
    code = cmd[2].replace("/tmp/nexus-worker-heartbeat", str(hb))
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
    old = time.time() - 600
    os.utime(hb, (old, old))
    assert subprocess.run([sys.executable, "-c", code]).returncode == 1
