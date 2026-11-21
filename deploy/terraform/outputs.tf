output "cluster_name" {
  description = "EKS cluster name (EKS_CLUSTER variable of the GitHub Environment)."
  value       = aws_eks_cluster.main.name
}

output "github_deploy_role_arn" {
  description = "Role for deploy.yml (AWS_DEPLOY_ROLE_ARN variable of the GitHub Environment)."
  value       = aws_iam_role.github_deploy.arn
}

output "app_role_arn" {
  description = "IRSA role: annotate the `nexus` ServiceAccount with eks.amazonaws.com/role-arn."
  value       = aws_iam_role.app.arn
}

output "documents_bucket" {
  description = "S3 bucket for document blobs (NEXUS_S3_BUCKET)."
  value       = aws_s3_bucket.documents.bucket
}

output "database_endpoint" {
  description = "PostgreSQL endpoint."
  value       = aws_db_instance.main.address
}

output "database_secret_arn" {
  description = "Secrets Manager secret holding the generated master credentials."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "redis_endpoint" {
  description = "Redis primary endpoint (TLS, rediss://)."
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "redis_auth_secret_arn" {
  description = "Secrets Manager secret holding the Redis AUTH token."
  value       = aws_secretsmanager_secret.redis.arn
}

output "backup_vault" {
  description = "AWS Backup vault with database recovery points."
  value       = aws_backup_vault.main.name
}

output "alarm_topic_arn" {
  description = "SNS topic for infrastructure alarms."
  value       = aws_sns_topic.alarms.arn
}
