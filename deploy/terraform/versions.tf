terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
  # Partial configuration: bucket/key/region/lock table are supplied by `terraform init -backend-config=...` (see .github/workflows/terraform.yml and docs/infrastructure-aws.md).
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "nexus"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# Disaster-recovery region (replicated backups and S3 objects). Always configured; resources that use it are gated by var.enable_dr.
provider "aws" {
  alias  = "dr"
  region = var.dr_region
  default_tags {
    tags = {
      Project     = "nexus"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
