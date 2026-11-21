#!/usr/bin/env python3
"""Static policy checks for deploy/terraform (HCL parsed with python-hcl2; no cloud access, no terraform binary needed).

This is NOT `terraform validate` or `plan` - provider schemas are not consulted, so attribute typos and provider-side constraints are only caught by CI's `terraform validate`.
What it does enforce: encryption, backups, public exposure, least-privilege IAM, OIDC trust scoping, DR gating, variable hygiene, tfvars consistency, documented scanner skips.
Exit 1 on errors; warnings (e.g. CHANGE-ME placeholders) are printed. `--strict` makes warnings fail."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import hcl2

ROOT = Path(__file__).resolve().parents[2]
TF_DIR = ROOT / "deploy" / "terraform"
SECRET_PATTERNS = [(re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"), (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
                   (re.compile(r"(?i)aws_secret_access_key\s*=\s*\"[^\"$]+\"")  , "AWS secret key")]


def norm(x):
    if isinstance(x, dict):
        return {norm(k): norm(v) for k, v in x.items() if k not in ("__comments__", "__is_block__")}
    if isinstance(x, list):
        return [norm(i) for i in x]
    if isinstance(x, str) and len(x) >= 2 and x[0] == '"' and x[-1] == '"':
        return x[1:-1]
    return x


def load_dir(d: Path) -> tuple[dict, dict[str, str]]:
    """-> ({kind: merged}, {filename: raw text}); resources keyed (type, name) in res."""
    mod = {"resource": {}, "data": {}, "variable": {}, "provider": [], "terraform": [], "locals": {}}
    raw = {}
    for f in sorted(d.glob("*.tf")):
        raw[f.name] = f.read_text()
        doc = norm(hcl2.loads(raw[f.name]))
        for blk in doc.get("resource", []):
            for t, names in blk.items():
                for n, body in names.items():
                    mod["resource"][(t, n)] = body
        for blk in doc.get("data", []):
            for t, names in blk.items():
                for n, body in names.items():
                    mod["data"][(t, n)] = body
        for blk in doc.get("variable", []):
            mod["variable"].update(blk)
        mod["provider"] += [(k, v) for blk in doc.get("provider", []) for k, v in blk.items()] if isinstance(doc.get("provider"), list) else list(doc.get("provider", {}).items())
        mod["terraform"] += doc.get("terraform", [])
    return mod, raw


def _true(v) -> bool:
    return v is True or v == "true"


def _false(v) -> bool:
    return v is False or v == "false"


def _truthy_or_var(v) -> bool:
    """True / var reference both acceptable where a value is configurable; a literal false is not."""
    return not _false(v) and v is not None


def check_module(mod: dict, raw: dict[str, str]) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    res = mod["resource"]

    def of(t):
        return {n: b for (tt, n), b in res.items() if tt == t}

    # ---- versions / backend
    tf = mod["terraform"][0] if mod["terraform"] else {}
    if "required_version" not in tf:
        errs.append("terraform.required_version is not set")
    provs = (tf.get("required_providers") or [{}])[0]
    for p in ("aws",):
        if not re.match(r"^~>\s*\d+\.\d+", str((provs.get(p) or {}).get("version", ""))):
            errs.append(f"provider {p} must be pinned with a pessimistic constraint (~> X.Y)")
    if not any("s3" in b for b in tf.get("backend", [])):
        errs.append("remote state: backend \"s3\" is not configured (local state would hold secrets and cannot be shared)")

    # ---- S3
    for name, b in of("aws_s3_bucket").items():
        ref = re.compile(r"^\$\{aws_s3_bucket\." + re.escape(name) + r"(\[\d+\])?\.")

        def attached(rtype, cond=lambda body: True):
            return any(ref.match(str(body.get("bucket", ""))) and cond(body) for body in of(rtype).values())
        if not attached("aws_s3_bucket_public_access_block", lambda x: all(_true(x.get(k)) for k in ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"))):
            errs.append(f"S3 bucket {name}: public access is not fully blocked")
        if not attached("aws_s3_bucket_versioning", lambda x: "Enabled" in str(x.get("versioning_configuration"))):
            errs.append(f"S3 bucket {name}: versioning is not enabled (no rollback/undelete)")
        if not attached("aws_s3_bucket_server_side_encryption_configuration", lambda x: "aws:kms" in str(x.get("rule"))):
            errs.append(f"S3 bucket {name}: no SSE-KMS encryption")

    # ---- RDS
    for name, b in of("aws_db_instance").items():
        w = f"RDS {name}"
        if not _true(b.get("storage_encrypted")):
            errs.append(f"{w}: storage_encrypted must be true")
        if not _false(b.get("publicly_accessible")):
            errs.append(f"{w}: publicly_accessible must be false")
        if "password" in b:
            errs.append(f"{w}: do not set `password`; use manage_master_user_password")
        if not _true(b.get("manage_master_user_password")):
            errs.append(f"{w}: manage_master_user_password must be true")
        if "backup_retention_period" not in b:
            errs.append(f"{w}: backup_retention_period not set (defaults can disable backups)")
        if _false(b.get("deletion_protection")) or "deletion_protection" not in b:
            errs.append(f"{w}: deletion_protection must be set (true or a variable)")
        if not _false(b.get("skip_final_snapshot")):
            errs.append(f"{w}: skip_final_snapshot must be false")
        if "multi_az" not in b or _false(b.get("multi_az")):
            errs.append(f"{w}: multi_az must be configurable (variable) or true")
        if not _true(b.get("iam_database_authentication_enabled")):
            warns.append(f"{w}: IAM database authentication is not enabled")

    # ---- Redis
    for name, b in of("aws_elasticache_replication_group").items():
        for k in ("at_rest_encryption_enabled", "transit_encryption_enabled"):
            if not _true(b.get(k)):
                errs.append(f"Redis {name}: {k} must be true")
        if "auth_token" not in b:
            errs.append(f"Redis {name}: auth_token missing")

    # ---- network exposure
    for name, b in of("aws_security_group").items():
        for ing in b.get("ingress", []) or []:
            if "0.0.0.0/0" in str(ing) or "::/0" in str(ing):
                errs.append(f"security group {name}: ingress from the whole internet")
    for name, b in of("aws_vpc_security_group_ingress_rule").items():
        if b.get("cidr_ipv4") == "0.0.0.0/0" or b.get("cidr_ipv6") == "::/0":
            errs.append(f"ingress rule {name}: open to the internet")
        if "cidr_ipv4" in b and not str(b["cidr_ipv4"]).startswith("${") and b["cidr_ipv4"] != "":
            warns.append(f"ingress rule {name}: CIDR-based rule; prefer security-group references")

    # ---- EKS
    for name, b in of("aws_eks_cluster").items():
        if "secrets" not in str(b.get("encryption_config")):
            errs.append(f"EKS {name}: Kubernetes secrets are not envelope-encrypted with KMS")
        if "audit" not in str(b.get("enabled_cluster_log_types")):
            errs.append(f"EKS {name}: audit logging disabled")
        vc = (b.get("vpc_config") or [{}])[0]
        if not _true(vc.get("endpoint_private_access")):
            errs.append(f"EKS {name}: private endpoint access must be enabled")
        if _true(vc.get("endpoint_public_access")) and "public_access_cidrs" not in vc:
            errs.append(f"EKS {name}: public endpoint without a CIDR allow-list")
    for name, b in of("aws_launch_template").items():
        mo = (b.get("metadata_options") or [{}])[0]
        if mo.get("http_tokens") != "required":
            errs.append(f"launch template {name}: IMDSv2 must be required")

    # ---- KMS and logs
    for name, b in of("aws_kms_key").items():
        if not _true(b.get("enable_key_rotation")):
            errs.append(f"KMS key {name}: rotation must be enabled")
    for name, b in of("aws_cloudwatch_log_group").items():
        try:
            days = int(b.get("retention_in_days", 0))
        except (TypeError, ValueError):
            days = 0
        if days < 365:
            errs.append(f"log group {name}: retention_in_days must be >= 365 (got {b.get('retention_in_days')})")
        if "kms_key_id" not in b:
            errs.append(f"log group {name}: not encrypted with KMS")

    # ---- IAM
    for t in ("aws_iam_role_policy", "aws_iam_policy"):
        for name, b in of(t).items():
            pol = str(b.get("policy", ""))
            if re.search(r'Action\s*=\s*"\*"', pol) or re.search(r'Action\s*=\s*\[\s*"\*"\s*\]', pol):
                errs.append(f"IAM policy {name}: Action \"*\"")
            if re.search(r'Resource\s*=\s*"\*"', pol):
                errs.append(f"IAM policy {name}: Resource \"*\" (scope to ARNs)")
    for name, b in of("aws_iam_role").items():
        trust = str(b.get("assume_role_policy", ""))
        if "sts:AssumeRoleWithWebIdentity" in trust and ":sub" not in trust:
            errs.append(f"IAM role {name}: web-identity trust without a `sub` condition (any workload could assume it)")
    for (t, name), b in mod["data"].items():
        if t != "aws_iam_policy_document":
            continue
        stmts = b.get("statement", [])
        if "token.actions.githubusercontent.com" in str(stmts) or "github" in str(stmts).lower():
            subs = [c for s in stmts for c in s.get("condition", []) if str(c.get("variable", "")).endswith(":sub")]
            if not subs:
                errs.append(f"policy document {name}: GitHub OIDC trust without a `sub` condition")
            for c in subs:
                for v in c.get("values", []):
                    if "*" in v or c.get("test") != "StringEquals":
                        errs.append(f"policy document {name}: OIDC `sub` must be an exact match (no wildcards / StringLike): {v!r}")
                    elif not re.match(r"^repo:.+:environment:.+$", v):
                        errs.append(f"policy document {name}: OIDC `sub` must pin repo AND environment: {v!r}")

    # ---- DR gating
    for (t, name), b in res.items():
        if "aws.dr" in str(b.get("provider", "")):
            if "var.enable_dr" not in str(b.get("count", "")):
                errs.append(f"{t}.{name}: uses the DR provider but is not gated by `count = var.enable_dr ? 1 : 0`")

    # ---- variables
    for vname, v in mod["variable"].items():
        if "type" not in v:
            errs.append(f"variable {vname}: no type")
        if not v.get("description"):
            errs.append(f"variable {vname}: no description")
        if re.search(r"(password|secret|token|api_key)", vname) and "default" in v:
            errs.append(f"variable {vname}: secrets must not have defaults")

    # ---- raw text
    for fn, text in raw.items():
        for rx, what in SECRET_PATTERNS:
            if rx.search(text):
                errs.append(f"{fn}: possible hard-coded {what}")
        for m in re.finditer(r"#\s*checkov:skip=([A-Z0-9_]+)(?::(.*))?", text):
            if len((m.group(2) or "").strip()) < 20:
                errs.append(f"{fn}: checkov skip {m.group(1)} needs a written justification")
    return errs, warns


def check_tfvars(mod: dict, tfvars_dir: Path) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    declared = mod["variable"]
    required = {k for k, v in declared.items() if "default" not in v}
    files = sorted(tfvars_dir.glob("*.tfvars"))
    if not files:
        return ["no environments/*.tfvars"], warns
    for f in files:
        vals = norm(hcl2.loads(f.read_text()))
        for k in vals:
            if k not in declared:
                errs.append(f"{f.name}: sets undeclared variable {k}")
        for k in required - set(vals):
            errs.append(f"{f.name}: required variable {k} is not set")
        if vals.get("environment") != f.stem:
            errs.append(f"{f.name}: environment must equal the file name ({vals.get('environment')!r})")
        if "CHANGE-ME" in f.read_text():
            warns.append(f"{f.name}: contains CHANGE-ME placeholders; set them before applying")
        if "0.0.0.0/0" in str(vals.get("eks_public_access_cidrs", "")):
            errs.append(f"{f.name}: Kubernetes API open to the internet")
        if f.stem == "production":
            if not _true(vals.get("db_multi_az", declared["db_multi_az"].get("default"))):
                errs.append("production must run Multi-AZ")
            if not _true(vals.get("db_deletion_protection", declared["db_deletion_protection"].get("default"))):
                errs.append("production must have deletion protection")
            if not _true(vals.get("enable_dr")):
                errs.append("production must enable DR replication")
            if int(vals.get("db_backup_retention_days", declared["db_backup_retention_days"].get("default", 0))) < 14:
                errs.append("production backup retention must be >= 14 days")
            if int(vals.get("az_count", declared["az_count"].get("default", 0))) < 3:
                warns.append("production should span 3 AZs")
        if vals.get("dr_region") and vals.get("dr_region") == vals.get("region"):
            errs.append(f"{f.name}: dr_region must differ from region")
    return errs, warns


def validate(tf_dir: Path | None = None) -> tuple[list[str], list[str]]:
    d = tf_dir or TF_DIR
    mod, raw = load_dir(d)
    e1, w1 = check_module(mod, raw)
    e2, w2 = check_tfvars(mod, d / "environments")
    sys.path.insert(0, str(Path(__file__).parent))
    from tffmt_align import misaligned

    e3 = [f"{fn}:{ln}: '=' alignment differs from terraform fmt style (run terraform fmt)" for fn, text in raw.items() for ln in misaligned(text)[:3]]
    return e1 + e2 + e3, w1 + w2


def main() -> int:
    errs, warns = validate()
    for w in warns:
        print("WARN:", w)
    for e in errs:
        print("FAIL:", e)
    bad = bool(errs) or (bool(warns) and "--strict" in sys.argv)
    print("terraform policy: " + ("FAILED" if bad else f"ok ({len(warns)} warnings)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
