"""Phase 18: static Terraform policy checks, proven by mutation on a scratch copy of deploy/terraform. These tests do NOT run terraform; they prove the repository's
own policy checks fire when a guard is removed, and that the shipped configuration satisfies them."""
from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infrastructure" / "scripts"))
spec = importlib.util.spec_from_file_location("validate_terraform", ROOT / "infrastructure" / "scripts" / "validate_terraform.py")
T = importlib.util.module_from_spec(spec)
sys.modules["validate_terraform"] = T
spec.loader.exec_module(T)
import tffmt_align as F  # noqa: E402


def test_shipped_terraform_passes_policy():
    errs, warns = T.validate()
    assert errs == []
    assert warns and all("CHANGE-ME" in w for w in warns)       # the only warnings are the placeholders the operator must fill in


@pytest.fixture
def tf(tmp_path):
    d = tmp_path / "tf"
    shutil.copytree(ROOT / "deploy" / "terraform", d)
    return d


def mutate(tf: Path, fname: str, old: str, new: str):
    p = tf / fname
    s = p.read_text()
    assert old in s, f"{old!r} not in {fname}"
    p.write_text(s.replace(old, new, 1))
    return T.validate(tf)[0]


@pytest.mark.parametrize("fname,old,new,needle", [
    ("storage.tf", "block_public_policy     = true", "block_public_policy     = false", "public access is not fully blocked"),
    ("storage.tf", 'status = "Enabled"\n  }\n}\n\nresource "aws_s3_bucket_server_side_encryption_configuration" "documents"', 'status = "Suspended"\n  }\n}\n\nresource "aws_s3_bucket_server_side_encryption_configuration" "documents"', "versioning"),
    ("storage.tf", 'sse_algorithm     = "aws:kms"\n      kms_master_key_id = aws_kms_key.main.arn', 'sse_algorithm = "AES256"', "no SSE-KMS"),
    ("data.tf", "storage_encrypted     = true", "storage_encrypted     = false", "storage_encrypted"),
    ("data.tf", "publicly_accessible    = false", "publicly_accessible    = true", "publicly_accessible"),
    ("data.tf", "manage_master_user_password   = true", "manage_master_user_password   = false", "manage_master_user_password"),
    ("data.tf", '  username                      = "nexus_admin"', '  username                      = "nexus_admin"\n  password = "hunter2"', "do not set `password`"),
    ("data.tf", "  backup_retention_period   = var.db_backup_retention_days\n", "", "backup_retention_period"),
    ("data.tf", "deletion_protection       = var.db_deletion_protection", "deletion_protection       = false", "deletion_protection"),
    ("data.tf", "skip_final_snapshot       = false", "skip_final_snapshot       = true", "skip_final_snapshot"),
    ("data.tf", "multi_az               = var.db_multi_az", "multi_az               = false", "multi_az"),
    ("data.tf", "at_rest_encryption_enabled = true", "at_rest_encryption_enabled = false", "at_rest_encryption_enabled"),
    ("data.tf", "transit_encryption_enabled = true", "transit_encryption_enabled = false", "transit_encryption_enabled"),
    ("data.tf", '  auth_token                 = random_password.redis.result\n', "", "auth_token missing"),
    ("data.tf", 'to_port                      = 5432\n', 'to_port                      = 5432\n  cidr_ipv4 = "0.0.0.0/0"\n', "open to the internet"),
    ("eks.tf", 'resources = ["secrets"]', 'resources = ["nothing"]', "envelope-encrypted"),
    ("eks.tf", '["api", "audit", "authenticator", "controllerManager", "scheduler"]', '["api"]', "audit logging"),
    ("eks.tf", "endpoint_private_access = true", "endpoint_private_access = false", "private endpoint"),
    ("eks.tf", "    public_access_cidrs     = var.eks_public_access_cidrs\n", "", "CIDR allow-list"),
    ("eks.tf", 'http_tokens                 = "required"', 'http_tokens                 = "optional"', "IMDSv2"),
    ("kms.tf", "enable_key_rotation     = true\n  deletion_window_in_days = 30\n  policy = jsonencode({\n    Version = \"2012-10-17\"\n    Statement = [\n      {\n        Sid       = \"AccountAdministration\"", "enable_key_rotation     = false\n  deletion_window_in_days = 30\n  policy = jsonencode({\n    Version = \"2012-10-17\"\n    Statement = [\n      {\n        Sid       = \"AccountAdministration\"", "rotation"),
    ("network.tf", "retention_in_days = 365", "retention_in_days = 30", "retention_in_days"),
    ("eks.tf", 'Action   = ["s3:ListBucket"]', 'Action   = "*"', 'Action "*"'),
    ("eks.tf", "Resource = aws_kms_key.main.arn\n      }\n    ]\n  })\n}", 'Resource = "*"\n      }\n    ]\n  })\n}', 'Resource "*"'),
    ("eks.tf", '"${local.eks_oidc}:sub" = "system:serviceaccount:nexus:nexus"', '"${local.eks_oidc}:xxx" = "system:serviceaccount:nexus:nexus"', "without a `sub` condition"),
    ("github_oidc.tf", 'values   = ["repo:${var.github_repository}:environment:${var.environment}"]', 'values   = ["repo:${var.github_repository}:*"]', "exact match"),
    ("github_oidc.tf", 'values   = ["repo:${var.github_repository}:environment:${var.environment}"]', 'values   = ["repo:${var.github_repository}:ref:refs/heads/main"]', "pin repo AND environment"),
    ("variables.tf", '  description = "Address subscribed to the alarm topic (confirmation e-mail is sent by SNS). Empty = topic without subscribers."\n', "", "no description"),
    ("variables.tf", 'variable "region" {\n  description = "Primary AWS region."\n  type        = string\n}', 'variable "region" {\n  description = "Primary AWS region."\n}', "no type"),
    ("versions.tf", 'version = "~> 5.60"', 'version = ">= 4.0"', "pinned with a pessimistic"),
    ("versions.tf", "required_version = \">= 1.6.0\"\n", "", "required_version"),
    ("versions.tf", '  backend "s3" {}\n', "", "remote state"),
])
def test_policy_fires_when_a_guard_is_removed(tf, fname, old, new, needle):
    errs = mutate(tf, fname, old, new)
    assert any(needle in e for e in errs), errs


def test_dr_resources_must_be_gated(tf):
    errs = mutate(tf, "kms.tf", '  count                   = var.enable_dr ? 1 : 0\n  provider                = aws.dr', '  provider                = aws.dr')
    assert any("not gated" in e for e in errs), errs


def test_hardcoded_credentials_and_unjustified_skips_are_rejected(tf):
    errs = mutate(tf, "versions.tf", 'provider "aws" {\n  region', 'provider "aws" {\n  access_key = "AKIAABCDEFGHIJKLMNOP"\n  region')
    assert any("hard-coded AWS access key" in e for e in errs)
    errs = mutate(tf, "eks.tf", "#checkov:skip=CKV_AWS_39:Public endpoint is deliberate", "#checkov:skip=CKV_AWS_39:x\n  #")
    assert any("needs a written justification" in e for e in errs)


def test_secret_variable_with_default_is_rejected(tf):
    (tf / "extra.tf").write_text('variable "db_password" {\n  description = "x"\n  type        = string\n  default     = "pw"\n}\n')
    assert any("secrets must not have defaults" in e for e in T.validate(tf)[0])


def test_kubernetes_api_cannot_be_opened_to_the_world(tf):
    assert any("open to the internet" in e for e in mutate(tf, "environments/production.tfvars", '["203.0.113.0/24"]', '["0.0.0.0/0"]'))


@pytest.mark.parametrize("key,value,needle", [
    ("db_multi_az", "false", "Multi-AZ"),
    ("db_deletion_protection", "false", "deletion protection"),
    ("enable_dr", "false", "enable DR"),
    ("db_backup_retention_days", "3", "retention"),
    ("dr_region", '"us-east-1"', "must differ"),
    ("environment", '"staging"', "must equal the file name"),
])
def test_production_tfvars_guards(tf, key, value, needle):
    p = tf / "environments" / "production.tfvars"
    text = p.read_text()
    assert re.search(rf"^{key}\s*=", text, re.M)
    p.write_text(re.sub(rf"^({key}\s*=\s*).*$", rf"\g<1>{value}", text, flags=re.M))
    errs = T.validate(tf)[0]
    assert any(needle.lower() in e.lower() for e in errs), errs


def test_tfvars_undeclared_and_missing_required(tf):
    p = tf / "environments" / "staging.tfvars"
    p.write_text(p.read_text() + 'bogus_setting = 1\n')
    assert any("undeclared variable bogus_setting" in e for e in T.validate(tf)[0])
    p.write_text(p.read_text().replace('region                  = "us-east-1"\n', ""))
    assert any("required variable region is not set" in e for e in T.validate(tf)[0])


def test_alignment_checker_matches_terraform_fmt_style():
    ok = 'resource "x" "y" {\n  a   = 1\n  bcd = 2\n\n  e = 3\n}\n'
    assert F.misaligned(ok) == []
    assert F.misaligned('resource "x" "y" {\n  a = 1\n  bcd = 2\n}\n') == [2]
    assert F.align('x {\n  a = 1\n  bcd = 2\n}') == 'x {\n  a   = 1\n  bcd = 2\n}'
    # multi-line values and heredocs end a group and are left alone
    s = 'x {\n  name = "n"\n  policy = jsonencode({\n    a = 1\n  })\n  b = <<EOT\n  q = 1\nEOT\n}\n'
    assert F.align(s) == s
    assert any("alignment" in e for e in mutate_raw_alignment())


def mutate_raw_alignment():
    d = Path(__import__("tempfile").mkdtemp()) / "tf"
    shutil.copytree(ROOT / "deploy" / "terraform", d)
    p = d / "outputs.tf"
    p.write_text(p.read_text().replace('  description = "EKS cluster name (EKS_CLUSTER variable of the GitHub Environment)."\n  value       =', '  description = "EKS cluster name (EKS_CLUSTER variable of the GitHub Environment)."\n  value =', 1))
    return T.validate(d)[0]


def test_every_aws_resource_type_is_in_the_provider_family_we_pin():
    mod, _ = T.load_dir(ROOT / "deploy" / "terraform")
    types = {t for t, _ in mod["resource"]}
    assert types and all(t.startswith(("aws_", "random_")) for t in types)
    assert {"aws_db_instance", "aws_eks_cluster", "aws_s3_bucket", "aws_elasticache_replication_group", "aws_backup_plan", "aws_kms_key"} <= types
