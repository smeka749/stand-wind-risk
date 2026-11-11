# Remote state for the main stack (created by ../../bootstrap).
# The bucket name contains the AWS account ID, so it is passed at init:
#   terraform init -backend-config="bucket=wind-risk-tfstate-<account-id>"
# CI's validate job runs `terraform init -backend=false`, which skips this.
terraform {
  backend "s3" {
    key            = "prod/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "wind-risk-tflock"
    encrypt        = true
  }
}
