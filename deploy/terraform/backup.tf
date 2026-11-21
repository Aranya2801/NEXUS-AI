# AWS Backup: daily + weekly recovery points of the database in a locked-down vault; copied to the DR region when enable_dr is set.
# RDS automated backups (point-in-time recovery, db_backup_retention_days) are separate and always on; these recovery points survive deletion of the instance.
resource "aws_backup_vault" "main" {
  name        = "${local.name}-vault"
  kms_key_arn = aws_kms_key.main.arn
}

resource "aws_backup_vault" "dr" {
  count       = var.enable_dr ? 1 : 0
  provider    = aws.dr
  name        = "${local.name}-vault-dr"
  kms_key_arn = aws_kms_key.dr[0].arn
}

resource "aws_backup_plan" "db" {
  name = "${local.name}-db"

  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 3 * * ? *)"
    start_window      = 60
    completion_window = 180
    lifecycle {
      delete_after = 35
    }
    dynamic "copy_action" {
      for_each = var.enable_dr ? [1] : []
      content {
        destination_vault_arn = aws_backup_vault.dr[0].arn
        lifecycle {
          delete_after = 35
        }
      }
    }
  }

  rule {
    rule_name         = "weekly-long-retention"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 4 ? * SUN *)"
    lifecycle {
      delete_after = 365
    }
    dynamic "copy_action" {
      for_each = var.enable_dr ? [1] : []
      content {
        destination_vault_arn = aws_backup_vault.dr[0].arn
        lifecycle {
          delete_after = 365
        }
      }
    }
  }
}

resource "aws_iam_role" "backup" {
  name = "${local.name}-backup"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "backup.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_backup_selection" "db" {
  name         = "database"
  plan_id      = aws_backup_plan.db.id
  iam_role_arn = aws_iam_role.backup.arn
  resources    = [aws_db_instance.main.arn]
}
