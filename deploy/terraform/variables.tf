variable "environment" {
  description = "Environment name (staging or production); used in resource names and tags."
  type        = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "region" {
  description = "Primary AWS region."
  type        = string
}

variable "dr_region" {
  description = "Disaster-recovery region for replicated backups/objects. Must differ from region."
  type        = string
  default     = "us-west-2"
}

variable "enable_dr" {
  description = "Replicate backups (AWS Backup copy) and document objects (S3 CRR) to dr_region. Strongly recommended for production."
  type        = bool
  default     = false
}

variable "vpc_cidr" {
  description = "CIDR of the VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to spread across (2 or 3)."
  type        = number
  default     = 3
  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "eks_version" {
  description = "Kubernetes version of the EKS control plane."
  type        = string
  default     = "1.30"
}

variable "eks_public_access_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint. Keep this to your office/VPN ranges; never 0.0.0.0/0."
  type        = list(string)
  validation {
    condition     = !contains(var.eks_public_access_cidrs, "0.0.0.0/0")
    error_message = "Do not expose the Kubernetes API to the whole internet."
  }
}

variable "node_instance_types" {
  description = "Instance types for the managed node group."
  type        = list(string)
  default     = ["m6i.large"]
}

variable "node_min_size" {
  description = "Minimum nodes."
  type        = number
  default     = 2
}

variable "node_max_size" {
  description = "Maximum nodes."
  type        = number
  default     = 6
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.m6i.large"
}

variable "db_allocated_storage_gb" {
  description = "Initial RDS storage (GiB); storage autoscaling is enabled up to db_max_allocated_storage_gb."
  type        = number
  default     = 100
}

variable "db_max_allocated_storage_gb" {
  description = "Upper bound for RDS storage autoscaling (GiB)."
  type        = number
  default     = 500
}

variable "db_multi_az" {
  description = "Run a synchronous standby in another AZ (automatic failover). Required for production."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Automated backup retention (point-in-time recovery window), 7-35 days."
  type        = number
  default     = 14
  validation {
    condition     = var.db_backup_retention_days >= 7 && var.db_backup_retention_days <= 35
    error_message = "Retention must be between 7 and 35 days."
  }
}

variable "db_deletion_protection" {
  description = "Block deletion of the database through the API. Disable only for throwaway environments."
  type        = bool
  default     = true
}

variable "redis_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.small"
}

variable "document_noncurrent_retention_days" {
  description = "Days to keep previous versions of document objects (the undelete/rollback window for S3)."
  type        = number
  default     = 90
}

variable "github_repository" {
  description = "GitHub repository (owner/name) allowed to assume the deploy roles through OIDC."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use the form owner/name."
  }
}

variable "alarm_email" {
  description = "Address subscribed to the alarm topic (confirmation e-mail is sent by SNS). Empty = topic without subscribers."
  type        = string
  default     = ""
}
