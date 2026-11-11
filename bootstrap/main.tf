# One-time AWS bootstrap for the wind risk pipeline. Apply once from a
# laptop with admin AWS credentials; its own state stays local (and is
# git-ignored). Creates:
#   - a monthly cost budget with email alerts (guards the $100 budget)
#   - an S3 bucket + DynamoDB table for the main stack's Terraform state
#   - a GitHub OIDC provider + a deploy role that ONLY pushes to main in
#     the listed repos can assume (no long-lived AWS keys in GitHub)
#
#   cd bootstrap
#   terraform init
#   terraform apply -var 'github_repos=["<owner>/<repo>"]' \
#                   -var 'budget_email=<you@example.com>'

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}
variable "github_repos" {
  description = "GitHub repos allowed to deploy, as owner/name."
  type        = list(string)
}
variable "github_repo_ids" {
  description = <<-EOT
    The same repos in GitHub's newer OIDC subject format, owner@<owner id>/name@<repo id>.
    Set them when the deploy fails with "Not authorized to perform
    sts:AssumeRoleWithWebIdentity"; CloudTrail shows the exact subject GitHub sent.
  EOT
  type        = list(string)
  default     = []
}
variable "budget_email" {
  description = "Email address for budget alerts."
  type        = string
}
variable "budget_limit_usd" {
  type    = number
  default = 100
}

provider "aws" { region = var.region }

data "aws_caller_identity" "current" {}

locals {
  tags         = { Project = "wind-risk", Purpose = "bootstrap" }
  state_bucket = "wind-risk-tfstate-${data.aws_caller_identity.current.account_id}"
}

# ------------------------------------------------------------ Budget
resource "aws_budgets_budget" "monthly" {
  name         = "wind-risk-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 90
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}

# ------------------------------------------------- Terraform state
resource "aws_s3_bucket" "tfstate" {
  bucket = local.state_bucket
  tags   = local.tags
}
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_dynamodb_table" "tflock" {
  name         = "wind-risk-tflock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  attribute {
    name = "LockID"
    type = "S"
  }
  tags = local.tags
}

# ------------------------------------------------ GitHub OIDC deploy
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # AWS no longer relies on these for GitHub's OIDC endpoint, but the
  # API still accepts/expects a list.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
  tags = local.tags
}

data "aws_iam_policy_document" "github_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Only pushes to main in the listed repos can deploy. Exact matches, no
    # wildcards: the ID form also stops a recreated repo with the same name.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        for r in concat(var.github_repos, var.github_repo_ids) :
        "repo:${r}:ref:refs/heads/main"
      ]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "wind-risk-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_trust.json
  tags               = local.tags
}

# Demo shortcut: the main stack creates IAM roles, networking, RDS,
# ECS and Batch, so the deploy role needs broad rights. For a real
# environment, replace this with a scoped policy (next step on slides).
resource "aws_iam_role_policy_attachment" "github_deploy_admin" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# ------------------------------------------------------------ Outputs
output "state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}
output "lock_table" {
  value = aws_dynamodb_table.tflock.name
}
output "deploy_role_arn" {
  description = "Set as the GitHub Actions variable AWS_ROLE_ARN."
  value       = aws_iam_role.github_deploy.arn
}
output "backend_init_command" {
  description = "Run in service/infra to switch the main stack to remote state."
  value       = "terraform init -migrate-state -backend-config=\"bucket=${aws_s3_bucket.tfstate.bucket}\""
}
