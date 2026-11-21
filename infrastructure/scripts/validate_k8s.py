#!/usr/bin/env python3
"""Static validator for deploy/k8s (no cluster, kubectl or kustomize needed). It checks the things that most often break a rollout or weaken a deployment:
resource wiring (selectors, ports, config/secret references, HPA/PDB/Ingress targets), pod hardening (non-root, read-only FS, dropped capabilities, seccomp,
resource limits, probes, pinned images) and that no real secrets are committed. It is NOT a replacement for `kubectl apply --dry-run=server` / `kubeconform`
– it cannot know CRD schemas or cluster state. Usage: validate_k8s.py [DIR ...]   (exit 1 on any violation)."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
SECRET_EXAMPLE = "secret.example.yaml"


def load_dir(d: Path) -> tuple[list[dict], list[str]]:
    errs: list[str] = []
    kfile = d / "kustomization.yaml"
    if not kfile.exists():
        return [], [f"{d}: no kustomization.yaml"]
    kz = yaml.safe_load(kfile.read_text())
    listed = [r for r in kz.get("resources", []) if not r.startswith("..")]
    docs: list[dict] = []
    for r in listed:
        f = d / r
        if not f.exists():
            errs.append(f"{kfile}: resource {r} does not exist")
            continue
        for doc in yaml.safe_load_all(f.read_text()):
            if doc:
                doc["_file"] = r
                docs.append(doc)
    on_disk = {p.name for p in d.glob("*.yaml")} - {"kustomization.yaml", SECRET_EXAMPLE}
    for orphan in sorted(on_disk - set(listed)):
        errs.append(f"{d}: {orphan} is not listed in kustomization.yaml (it would silently not be deployed)")
    for doc in docs:
        if doc.get("kind") == "Secret":
            errs.append(f"{d}: {doc['_file']} deploys a Secret; secrets must be created out of band")
    for img in kz.get("images", []):
        if str(img.get("newTag", "")).lower() in ("", "latest"):
            errs.append(f"{kfile}: image override {img.get('name')} must pin a real tag")
    return docs, errs


def _labels_match(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def validate(docs: list[dict], *, secret_names: set[str], known_env: set[str] | None = None, passthrough_env: set[str] | None = None,
             probe_paths: set[str] | None = None) -> list[str]:
    errs: list[str] = []
    by = lambda kind: [d for d in docs if d.get("kind") == kind]  # noqa: E731
    cms = {d["metadata"]["name"]: d for d in by("ConfigMap")}
    sas = {d["metadata"]["name"] for d in by("ServiceAccount")}
    deployments = {d["metadata"]["name"]: d for d in by("Deployment")}
    pods = [(d, d["spec"]["template"]) for d in docs if d.get("kind") in WORKLOADS]

    for d in docs:
        for f in ("apiVersion", "kind"):
            if f not in d:
                errs.append(f"{d.get('_file')}: object without {f}")
        if not (d.get("metadata") or {}).get("name"):
            errs.append(f"{d.get('_file')}: {d.get('kind')} without metadata.name")

    # ---- pods
    for w, tpl in pods:
        n, spec = f"{w['kind']}/{w['metadata']['name']}", tpl["spec"]
        tlabels = tpl["metadata"].get("labels", {})
        if w["kind"] != "Job" and not _labels_match(w["spec"]["selector"].get("matchLabels", {}), tlabels):
            errs.append(f"{n}: selector.matchLabels does not match the pod template labels")
        for bad in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(bad):
                errs.append(f"{n}: {bad} is enabled")
        if spec.get("automountServiceAccountToken") is not False:
            errs.append(f"{n}: automountServiceAccountToken must be false")
        if spec.get("serviceAccountName") and spec["serviceAccountName"] not in sas:
            errs.append(f"{n}: serviceAccount {spec['serviceAccountName']} is not defined")
        psc = spec.get("securityContext", {})
        if psc.get("seccompProfile", {}).get("type") != "RuntimeDefault":
            errs.append(f"{n}: pod seccompProfile must be RuntimeDefault")
        for c in spec["containers"]:
            cn = f"{n}/{c['name']}"
            image = c["image"]
            tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
            if "@sha256:" not in image and (not tag or tag == "latest"):
                errs.append(f"{cn}: image must have a pinned tag or digest (got {image})")
            sc = c.get("securityContext", {})
            if sc.get("allowPrivilegeEscalation") is not False:
                errs.append(f"{cn}: allowPrivilegeEscalation must be false")
            if sc.get("privileged"):
                errs.append(f"{cn}: privileged")
            if sc.get("readOnlyRootFilesystem") is not True:
                errs.append(f"{cn}: readOnlyRootFilesystem must be true")
            if not (sc.get("runAsNonRoot") or psc.get("runAsNonRoot")):
                errs.append(f"{cn}: runAsNonRoot must be true")
            if "ALL" not in sc.get("capabilities", {}).get("drop", []):
                errs.append(f"{cn}: must drop ALL capabilities")
            res = c.get("resources", {})
            for kind in ("requests", "limits"):
                for r in ("cpu", "memory"):
                    if r not in res.get(kind, {}):
                        errs.append(f"{cn}: resources.{kind}.{r} missing")
            if c.get("ports") and "readinessProbe" not in c:
                errs.append(f"{cn}: serves traffic but has no readinessProbe")
            if w["kind"] == "Deployment" and "livenessProbe" not in c:
                errs.append(f"{cn}: no livenessProbe")
            for pr in ("readinessProbe", "livenessProbe", "startupProbe"):
                path = (c.get(pr) or {}).get("httpGet", {}).get("path")
                if path and probe_paths is not None and path.startswith("/health") and path not in probe_paths:
                    errs.append(f"{cn}: {pr} path {path} is not a route of the application")
            for ef in c.get("envFrom", []):
                if "configMapRef" in ef and ef["configMapRef"]["name"] not in cms:
                    errs.append(f"{cn}: ConfigMap {ef['configMapRef']['name']} is not defined")
                if "secretRef" in ef and ef["secretRef"]["name"] not in secret_names:
                    errs.append(f"{cn}: Secret {ef['secretRef']['name']} is neither defined nor listed in {SECRET_EXAMPLE}")
            mounts = {m["name"] for m in c.get("volumeMounts", [])}
            vols = {v["name"] for v in spec.get("volumes", [])}
            if mounts - vols:
                errs.append(f"{cn}: volumeMounts without volumes: {sorted(mounts - vols)}")
        if w["kind"] == "Deployment" and w["spec"].get("strategy", {}).get("rollingUpdate", {}).get("maxUnavailable") == 0 and w["spec"].get("replicas", 1) < 2:
            pass  # single replica with maxUnavailable 0 is fine (surge handles it)

    # ---- ConfigMap keys must be real settings
    if known_env is not None:
        for name, cm in cms.items():
            for k in cm.get("data", {}):
                if k not in known_env and k not in (passthrough_env or set()):
                    errs.append(f"ConfigMap/{name}: {k} is not a NEXUS_ setting of this application (typo?)")

    # ---- services
    for s in by("Service"):
        sel = s["spec"].get("selector", {})
        targets = [(d, t) for d, t in pods if _labels_match(sel, t["metadata"].get("labels", {}))]
        if not targets:
            errs.append(f"Service/{s['metadata']['name']}: selector matches no workload")
            continue
        names = {p["name"]: p["containerPort"] for _, t in targets for c in t["spec"]["containers"] for p in c.get("ports", [])}
        for p in s["spec"]["ports"]:
            tp = p["targetPort"]
            if isinstance(tp, str) and tp not in names:
                errs.append(f"Service/{s['metadata']['name']}: targetPort {tp} is not a named containerPort")
            if isinstance(tp, int) and tp not in names.values():
                errs.append(f"Service/{s['metadata']['name']}: targetPort {tp} is not exposed by the pods")

    # ---- ingress
    services = {s["metadata"]["name"]: s for s in by("Service")}
    for ing in by("Ingress"):
        for rule in ing["spec"].get("rules", []):
            for path in rule["http"]["paths"]:
                svc = path["backend"]["service"]
                if svc["name"] not in services:
                    errs.append(f"Ingress/{ing['metadata']['name']}: backend service {svc['name']} is not defined")
                    continue
                ports = [p for p in services[svc["name"]]["spec"]["ports"]]
                want = svc["port"].get("name") or svc["port"].get("number")
                if not any(want in (p.get("name"), p["port"]) for p in ports):
                    errs.append(f"Ingress/{ing['metadata']['name']}: service {svc['name']} has no port {want}")
        if not ing["spec"].get("tls"):
            errs.append(f"Ingress/{ing['metadata']['name']}: no TLS section")

    # ---- HPA / PDB / NetworkPolicy targets
    for h in by("HorizontalPodAutoscaler"):
        tgt = h["spec"]["scaleTargetRef"]["name"]
        n = f"HPA/{h['metadata']['name']}"
        if tgt not in deployments:
            errs.append(f"{n}: scaleTargetRef {tgt} is not a Deployment here")
            continue
        if h["spec"]["minReplicas"] > h["spec"]["maxReplicas"]:
            errs.append(f"{n}: minReplicas > maxReplicas")
        if "replicas" in deployments[tgt]["spec"] and h["spec"]["minReplicas"] > deployments[tgt]["spec"]["replicas"]:
            errs.append(f"{n}: minReplicas exceeds the Deployment's replicas")
        cpu_metric = any(m.get("resource", {}).get("name") == "cpu" for m in h["spec"].get("metrics", []))
        if cpu_metric:
            for c in deployments[tgt]["spec"]["template"]["spec"]["containers"]:
                if "cpu" not in c.get("resources", {}).get("requests", {}):
                    errs.append(f"{n}: CPU utilisation scaling needs resources.requests.cpu on {c['name']}")
    for p in by("PodDisruptionBudget"):
        sel = p["spec"]["selector"]["matchLabels"]
        hit = [d for d, t in pods if _labels_match(sel, t["metadata"].get("labels", {}))]
        if not hit:
            errs.append(f"PDB/{p['metadata']['name']}: selector matches no workload")
        elif any(d["spec"].get("replicas", 1) < 2 for d in hit) and p["spec"].get("minAvailable", 0) >= 1:
            errs.append(f"PDB/{p['metadata']['name']}: minAvailable>=1 with a single replica blocks node drains")
    for np_ in by("NetworkPolicy"):
        sel = np_["spec"].get("podSelector", {}).get("matchLabels", {})
        if sel and not any(_labels_match(sel, t["metadata"].get("labels", {})) for _, t in pods):
            errs.append(f"NetworkPolicy/{np_['metadata']['name']}: podSelector matches no workload")
    if not any(np_["spec"].get("podSelector") == {} and "Ingress" in np_["spec"].get("policyTypes", []) and not np_["spec"].get("ingress") for np_ in by("NetworkPolicy")):
        errs.append("no default-deny ingress NetworkPolicy")
    return errs


def validate_secret_example(path: Path) -> list[str]:
    errs = []
    doc = yaml.safe_load(path.read_text())
    for k, v in (doc.get("stringData") or {}).items():
        if not str(v).startswith("CHANGE_ME"):
            errs.append(f"{path.name}: {k} must be a CHANGE_ME placeholder, not a value")
    return errs


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[2] / "deploy" / "k8s"
    base = root / "base"
    docs, errs = load_dir(base)
    secret_names = set()
    ex = base / SECRET_EXAMPLE
    if ex.exists():
        errs += validate_secret_example(ex)
        secret_names.add(yaml.safe_load(ex.read_text())["metadata"]["name"])
    errs += validate(docs, secret_names=secret_names)
    mdocs, merrs = load_dir(root / "migrate")
    errs += merrs + validate(docs + mdocs, secret_names=secret_names)
    for e in errs:
        print("FAIL", e)
    print(f"{len(docs) + len(mdocs)} objects checked, {len(errs)} problem(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
