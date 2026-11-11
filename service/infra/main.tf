# Wind Risk Pipeline, one production-shaped environment (prod).
#
#   ALB -> ECS Fargate API; RDS Postgres holds quote state; private S3 artifacts.
#   Batch queues per stage: PreProcess and PostProcess on Fargate, Simulation
#   on EC2 (OpenFOAM + MPI). ECR, CloudWatch alarms -> SNS, per-role IAM.
#
# Applied by .github/workflows/deploy.yml. Further environments reuse this
# stack with a different var.environment and state key.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.0" }
  }
}

variable "region" {
  description = "AWS region for the one environment."
  default     = "us-west-2"
}
variable "environment" {
  description = "Environment name; one environment is built."
  default     = "prod"
}
variable "api_image" {
  description = "ECR image for the Quote Service API (tag :latest after first push)."
  default     = null
  type        = string
}
variable "image_tag" {
  description = "Tag for all four images (CI passes the git SHA)."
  type        = string
  default     = "latest"
}
variable "alert_email" {
  description = "Email for CloudWatch alarm notifications (SNS). Empty = no subscription."
  type        = string
  default     = ""
}
variable "simulation_capacity" {
  description = "EC2 (on-demand, default) or SPOT (cheaper; a reclaimed instance retries the job)."
  type        = string
  default     = "EC2"
  validation {
    condition     = contains(["SPOT", "EC2"], var.simulation_capacity)
    error_message = "simulation_capacity must be SPOT or EC2."
  }
}
variable "simulation_max_vcpus" {
  description = "Cap on simulation vCPUs: 800 = 50 concurrent 16-vCPU sims. Keep within the account's EC2 quota."
  type        = number
  default     = 800
}
variable "simulation_job_vcpus" {
  description = "vCPUs per simulation job. MPI ranks come from simulation_np."
  type        = number
  default     = 16
}
variable "simulation_np" {
  description = "MPI ranks for meshing and the solver (SME default 16)."
  type        = number
  default     = 16
}
variable "simulation_job_memory_mib" {
  description = "Memory per simulation job; leave headroom for the ECS agent on the instance."
  type        = number
  default     = 28672
}
variable "fargate_max_vcpus" {
  description = "Cap on Fargate vCPUs for each of the PreProcess and PostProcess compute environments."
  type        = number
  default     = 16
}
variable "simulation_instance_types" {
  description = "EC2 instance types for the Simulation Batch queue (sized to simulation_job_vcpus)."
  type        = list(string)
  default     = ["c6i.4xlarge"]
}

provider "aws" { region = var.region }

locals {
  name = "wind-risk-${var.environment}"
  tags = { Project = "wind-risk", Environment = var.environment }
  # S3 names are global across accounts: suffix with the account ID.
  bucket = "${local.name}-artifacts-${data.aws_caller_identity.current.account_id}"
}

data "aws_caller_identity" "current" {}
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# ---------------------------------------------------------------- S3
resource "aws_s3_bucket" "artifacts" {
  bucket = local.bucket
  tags   = local.tags
}
resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ------------------------------------------------------- DB credentials
# Generated password; tasks get it as an env var from this secret.
resource "random_password" "db" {
  length  = 32
  special = false
}
resource "aws_secretsmanager_secret" "db_password" {
  name = "${local.name}/db-password"
  tags = local.tags
}
resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = random_password.db.result
}

# ---------------------------------------------------------------- RDS
resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = data.aws_subnets.default.ids
  tags       = local.tags
}
resource "aws_security_group" "rds" {
  name   = "${local.name}-rds"
  vpc_id = data.aws_vpc.default.id
  tags   = local.tags
  # Ingress is added below, from the API and Batch security groups only.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_db_instance" "quotes" {
  identifier              = "${local.name}-quotes"
  engine                  = "postgres"
  engine_version          = "16" # major only; RDS picks the current 16.x minor
  instance_class          = "db.t3.micro"
  allocated_storage       = 20
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "windrisk"
  username                = "windrisk"
  password                = random_password.db.result
  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.rds.id]
  publicly_accessible     = false
  multi_az                = false
  skip_final_snapshot     = true
  deletion_protection     = false # one demo environment; enable for real prod
  backup_retention_period = 7
  tags                    = local.tags
}

# ---------------------------------------------------------------- ECR
resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  tags = local.tags
}
resource "aws_ecr_repository" "preprocess" {
  name                 = "${local.name}-preprocess"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  tags = local.tags
}
resource "aws_ecr_repository" "simulation" {
  name                 = "${local.name}-simulation"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  tags = local.tags
}
resource "aws_ecr_repository" "postprocess" {
  name                 = "${local.name}-postprocess"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  tags = local.tags
}
resource "aws_ecr_lifecycle_policy" "keep_last_10" {
  for_each = {
    api         = aws_ecr_repository.api.name
    preprocess  = aws_ecr_repository.preprocess.name
    simulation  = aws_ecr_repository.simulation.name
    postprocess = aws_ecr_repository.postprocess.name
  }
  repository = each.value
  # Deploys push a new simulation:<sha> image each time; without rule 1
  # the pinned OpenFOAM base (base-<sha>) aged out of "last 10". ECR never
  # lets a lower-priority rule expire an image a higher one matched.
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1, description = "keep the last 3 OpenFOAM base images",
        selection    = { tagStatus = "tagged", tagPrefixList = ["base-"], countType = "imageCountMoreThan", countNumber = 3 },
        action       = { type = "expire" },
      },
      {
        rulePriority = 2, description = "keep last 10 images",
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 },
        action       = { type = "expire" },
      },
    ]
  })
}

# ---------------------------------------------------------------- IAM
# Least privilege: each role gets this bucket and the DB secret, nothing more.
data "aws_iam_policy_document" "s3_artifacts" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db_password.arn]
  }
}
resource "aws_iam_policy" "artifacts_access" {
  name   = "${local.name}-artifacts-access"
  policy = data.aws_iam_policy_document.s3_artifacts.json
  tags   = local.tags
}
resource "aws_iam_role" "ecs_execution" {
  name = "${local.name}-ecs-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" },
      Action = "sts:AssumeRole",
    }],
  })
  tags = local.tags
}
resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy_attachment" "ecs_execution_secrets" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = aws_iam_policy.artifacts_access.arn
}
resource "aws_iam_role" "ecs_task" {
  name = "${local.name}-ecs-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" },
      Action = "sts:AssumeRole",
    }],
  })
  tags = local.tags
}
resource "aws_iam_role_policy_attachment" "ecs_task_artifacts" {
  role       = aws_iam_role.ecs_task.name
  policy_arn = aws_iam_policy.artifacts_access.arn
}
resource "aws_iam_role" "batch_service" {
  name = "${local.name}-batch-service"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow", Principal = { Service = "batch.amazonaws.com" },
      Action = "sts:AssumeRole",
    }],
  })
  tags = local.tags
}
resource "aws_iam_role_policy_attachment" "batch_service_managed" {
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}
resource "aws_iam_role" "batch_job" {
  for_each = toset(["preprocess", "simulation", "postprocess"])
  name     = "${local.name}-batch-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" },
      Action = "sts:AssumeRole",
    }],
  })
  tags = local.tags
}
resource "aws_iam_role_policy_attachment" "batch_job_artifacts" {
  for_each   = aws_iam_role.batch_job
  role       = each.value.name
  policy_arn = aws_iam_policy.artifacts_access.arn
}
resource "aws_iam_role" "batch_instance" {
  name = "${local.name}-batch-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" },
      Action = "sts:AssumeRole",
    }],
  })
  tags = local.tags
}
resource "aws_iam_role_policy_attachment" "batch_instance_managed" {
  role       = aws_iam_role.batch_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}
resource "aws_iam_instance_profile" "batch_instance" {
  name = "${local.name}-batch-instance"
  role = aws_iam_role.batch_instance.name
  tags = local.tags
}
resource "aws_iam_role" "batch_spot_fleet" {
  name = "${local.name}-batch-spot-fleet"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow", Principal = { Service = "spotfleet.amazonaws.com" },
      Action = "sts:AssumeRole",
    }],
  })
  tags = local.tags
}
resource "aws_iam_role_policy_attachment" "batch_spot_fleet_managed" {
  role       = aws_iam_role.batch_spot_fleet.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
}

# ------------------------------------------------------- CloudWatch logs
resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/ecs/${local.name}-api"
  retention_in_days = 30
  tags              = local.tags
}
resource "aws_cloudwatch_log_group" "batch" {
  for_each          = toset(["preprocess", "simulation", "postprocess"])
  name              = "/aws/batch/${local.name}-${each.key}"
  retention_in_days = 30
  tags              = local.tags
}
# Metric filter on {"event": "stage_failed"} log lines, for an alarm.
resource "aws_cloudwatch_log_metric_filter" "stage_failed" {
  for_each       = aws_cloudwatch_log_group.batch
  name           = "${local.name}-stage-failed-${each.key}"
  pattern        = "{ $.event = \"stage_failed\" }"
  log_group_name = each.value.name
  metric_transformation {
    name      = "${local.name}-stage-failed-${each.key}"
    namespace = "WindRisk"
    value     = "1"
  }
}
resource "aws_cloudwatch_metric_alarm" "stage_failed" {
  for_each            = aws_cloudwatch_log_metric_filter.stage_failed
  alarm_name          = "${local.name}-stage-failed-${each.key}"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = each.value.metric_transformation[0].name
  namespace           = "WindRisk"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  alarm_actions       = [aws_sns_topic.alerts.arn]
  alarm_description   = "A pipeline stage failed (see quote_id in the log event)."
  tags                = local.tags
}

# ------------------------------------------------------------- ECS API
resource "aws_security_group" "alb" {
  name   = "${local.name}-alb"
  vpc_id = data.aws_vpc.default.id
  tags   = local.tags
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_security_group" "api" {
  name   = "${local.name}-api"
  vpc_id = data.aws_vpc.default.id
  tags   = local.tags
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_security_group" "batch" {
  name   = "${local.name}-batch"
  vpc_id = data.aws_vpc.default.id
  tags   = local.tags
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_security_group_rule" "rds_from_api" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.rds.id
  source_security_group_id = aws_security_group.api.id
}
resource "aws_security_group_rule" "rds_from_batch" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.rds.id
  source_security_group_id = aws_security_group.batch.id
}
resource "aws_ecs_cluster" "main" {
  name = local.name
  tags = local.tags
}
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn
  container_definitions = jsonencode([{
    name         = "api"
    image        = coalesce(var.api_image, "${aws_ecr_repository.api.repository_url}:${var.image_tag}")
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment = [
      { name = "DB_HOST", value = aws_db_instance.quotes.address },
      { name = "DB_NAME", value = "windrisk" },
      { name = "DB_USER", value = "windrisk" },
      { name = "S3_BUCKET", value = aws_s3_bucket.artifacts.bucket },
      { name = "IMAGE_TAG", value = var.image_tag },
      { name = "LOG_LEVEL", value = "INFO" },
      # All three: a repeat POST retries a quote at whichever stage it is in.
      { name = "BATCH_QUEUE_PREPROCESS", value = aws_batch_job_queue.preprocess.name },
      { name = "BATCH_JOBDEF_PREPROCESS", value = aws_batch_job_definition.preprocess.name },
      { name = "BATCH_QUEUE_SIMULATION", value = aws_batch_job_queue.simulation.name },
      { name = "BATCH_JOBDEF_SIMULATION", value = aws_batch_job_definition.simulation.name },
      { name = "BATCH_QUEUE_POSTPROCESS", value = aws_batch_job_queue.postprocess.name },
      { name = "BATCH_JOBDEF_POSTPROCESS", value = aws_batch_job_definition.postprocess.name },
    ]
    secrets = [{ name = "DB_PASSWORD", valueFrom = aws_secretsmanager_secret.db_password.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 10
    }
  }])
  tags = local.tags
}
resource "aws_lb" "api" {
  name               = "${local.name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids
  tags               = local.tags
}
resource "aws_lb_target_group" "api" {
  name        = "${local.name}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id
  health_check {
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
  }
  tags = local.tags
}
resource "aws_lb_listener" "api" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
  tags = local.tags
}
resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
  depends_on = [aws_lb_listener.api]
  tags       = local.tags
}
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  dimensions          = { LoadBalancer = aws_lb.api.arn_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  alarm_description   = "API target returned 5xx."
  tags                = local.tags
}
resource "aws_cloudwatch_metric_alarm" "rds_free_storage" {
  alarm_name          = "${local.name}-rds-free-storage"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FreeStorageSpace"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 2000000000 # 2 GB
  dimensions          = { DBInstanceIdentifier = aws_db_instance.quotes.identifier }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  alarm_description   = "RDS free storage below 2 GB."
  tags                = local.tags
}

# ------------------------------------------------------------- Batch
resource "aws_batch_compute_environment" "preprocess" {
  compute_environment_name = "${local.name}-preprocess"
  # Batch checks the service role once at creation; attach the policy first.
  depends_on   = [aws_iam_role_policy_attachment.batch_service_managed]
  type         = "MANAGED"
  state        = "ENABLED"
  service_role = aws_iam_role.batch_service.arn
  compute_resources {
    type               = "FARGATE"
    max_vcpus          = var.fargate_max_vcpus
    subnets            = data.aws_subnets.default.ids
    security_group_ids = [aws_security_group.batch.id]
  }
  tags = local.tags
}
resource "aws_batch_compute_environment" "postprocess" {
  compute_environment_name = "${local.name}-postprocess"
  # Batch checks the service role once at creation; attach the policy first.
  depends_on   = [aws_iam_role_policy_attachment.batch_service_managed]
  type         = "MANAGED"
  state        = "ENABLED"
  service_role = aws_iam_role.batch_service.arn
  compute_resources {
    type               = "FARGATE"
    max_vcpus          = var.fargate_max_vcpus
    subnets            = data.aws_subnets.default.ids
    security_group_ids = [aws_security_group.batch.id]
  }
  tags = local.tags
}
resource "aws_batch_compute_environment" "simulation" {
  compute_environment_name = "${local.name}-simulation"
  # Batch checks the service role once at creation; attach the policy first.
  depends_on   = [aws_iam_role_policy_attachment.batch_service_managed]
  type         = "MANAGED"
  state        = "ENABLED"
  service_role = aws_iam_role.batch_service.arn
  compute_resources {
    type                = var.simulation_capacity
    allocation_strategy = "BEST_FIT"
    min_vcpus           = 0
    max_vcpus           = var.simulation_max_vcpus
    desired_vcpus       = 0
    instance_type       = var.simulation_instance_types
    instance_role       = aws_iam_instance_profile.batch_instance.arn
    # Spot-only settings; null for on-demand.
    spot_iam_fleet_role = var.simulation_capacity == "SPOT" ? aws_iam_role.batch_spot_fleet.arn : null
    bid_percentage      = var.simulation_capacity == "SPOT" ? 100 : null
    subnets             = data.aws_subnets.default.ids
    security_group_ids  = [aws_security_group.batch.id]
    tags                = local.tags
  }
  tags = local.tags
}
resource "aws_batch_job_queue" "preprocess" {
  name     = "${local.name}-preprocess"
  state    = "ENABLED"
  priority = 1
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.preprocess.arn
  }
  tags = local.tags
}
resource "aws_batch_job_queue" "simulation" {
  name     = "${local.name}-simulation"
  state    = "ENABLED"
  priority = 1
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.simulation.arn
  }
  tags = local.tags
}
resource "aws_batch_job_queue" "postprocess" {
  name     = "${local.name}-postprocess"
  state    = "ENABLED"
  priority = 1
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.postprocess.arn
  }
  tags = local.tags
}
# One job definition per stage. Batch timeout 3600s; the runners' 1500s
# timeouts sit inside it and under the 30-minute claim lease.
resource "aws_batch_job_definition" "preprocess" {
  name                  = "${local.name}-preprocess"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  container_properties = jsonencode({
    image            = "${aws_ecr_repository.preprocess.repository_url}:${var.image_tag}"
    executionRoleArn = aws_iam_role.ecs_execution.arn
    jobRoleArn       = aws_iam_role.batch_job["preprocess"].arn
    resourceRequirements = [
      { type = "VCPU", value = "2" },
      { type = "MEMORY", value = "4096" },
    ]
    networkConfiguration = { assignPublicIp = "ENABLED" }
    environment = [
      { name = "DB_HOST", value = aws_db_instance.quotes.address },
      { name = "DB_NAME", value = "windrisk" },
      { name = "DB_USER", value = "windrisk" },
      { name = "S3_BUCKET", value = aws_s3_bucket.artifacts.bucket },
      { name = "IMAGE_TAG", value = var.image_tag },
      { name = "LOG_LEVEL", value = "INFO" },
      { name = "BATCH_QUEUE_SIMULATION", value = aws_batch_job_queue.simulation.name },
      { name = "BATCH_JOBDEF_SIMULATION", value = aws_batch_job_definition.simulation.name },
    ]
    secrets = [{ name = "DB_PASSWORD", valueFrom = aws_secretsmanager_secret.db_password.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.batch["preprocess"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "preprocess"
      }
    }
  })
  timeout { attempt_duration_seconds = 3600 }
  # Retries infrastructure failures only; a stage Fail exits 0.
  retry_strategy { attempts = 3 }
  tags = local.tags
}
resource "aws_batch_job_definition" "simulation" {
  name = "${local.name}-simulation"
  type = "container"
  container_properties = jsonencode({
    image            = "${aws_ecr_repository.simulation.repository_url}:${var.image_tag}"
    executionRoleArn = aws_iam_role.ecs_execution.arn
    jobRoleArn       = aws_iam_role.batch_job["simulation"].arn
    resourceRequirements = [
      { type = "VCPU", value = tostring(var.simulation_job_vcpus) },
      # Leave headroom below the instance's memory for the ECS agent;
      # asking for all of it leaves the job RUNNABLE forever.
      { type = "MEMORY", value = tostring(var.simulation_job_memory_mib) },
    ]
    # OpenMPI needs more than the 64 MB default /dev/shm.
    linuxParameters = { sharedMemorySize = 1024 }
    environment = [
      { name = "DB_HOST", value = aws_db_instance.quotes.address },
      { name = "DB_NAME", value = "windrisk" },
      { name = "DB_USER", value = "windrisk" },
      { name = "S3_BUCKET", value = aws_s3_bucket.artifacts.bucket },
      { name = "IMAGE_TAG", value = var.image_tag },
      { name = "SIM_NP", value = tostring(var.simulation_np) },
      { name = "LOG_LEVEL", value = "INFO" },
      { name = "BATCH_QUEUE_POSTPROCESS", value = aws_batch_job_queue.postprocess.name },
      { name = "BATCH_JOBDEF_POSTPROCESS", value = aws_batch_job_definition.postprocess.name },
    ]
    secrets = [{ name = "DB_PASSWORD", valueFrom = aws_secretsmanager_secret.db_password.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.batch["simulation"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "simulation"
      }
    }
  })
  timeout { attempt_duration_seconds = 3600 }
  # Retries infrastructure failures only; a stage Fail exits 0.
  retry_strategy { attempts = 3 }
  tags = local.tags
}
resource "aws_batch_job_definition" "postprocess" {
  name                  = "${local.name}-postprocess"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  container_properties = jsonencode({
    image            = "${aws_ecr_repository.postprocess.repository_url}:${var.image_tag}"
    executionRoleArn = aws_iam_role.ecs_execution.arn
    jobRoleArn       = aws_iam_role.batch_job["postprocess"].arn
    resourceRequirements = [
      { type = "VCPU", value = "4" },
      { type = "MEMORY", value = "8192" },
    ]
    networkConfiguration = { assignPublicIp = "ENABLED" }
    environment = [
      { name = "DB_HOST", value = aws_db_instance.quotes.address },
      { name = "DB_NAME", value = "windrisk" },
      { name = "DB_USER", value = "windrisk" },
      { name = "S3_BUCKET", value = aws_s3_bucket.artifacts.bucket },
      { name = "IMAGE_TAG", value = var.image_tag },
      { name = "LOG_LEVEL", value = "INFO" },
    ]
    secrets = [{ name = "DB_PASSWORD", valueFrom = aws_secretsmanager_secret.db_password.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.batch["postprocess"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "postprocess"
      }
    }
  })
  timeout { attempt_duration_seconds = 3600 }
  # Retries infrastructure failures only; a stage Fail exits 0.
  retry_strategy { attempts = 3 }
  tags = local.tags
}


# Only these roles may submit, and only to this environment's queues/jobdefs.
data "aws_iam_policy_document" "submit_jobs" {
  statement {
    actions = ["batch:SubmitJob"]
    resources = [
      aws_batch_job_queue.preprocess.arn,
      aws_batch_job_queue.simulation.arn,
      aws_batch_job_queue.postprocess.arn,
      "arn:aws:batch:${var.region}:${data.aws_caller_identity.current.account_id}:job-definition/${local.name}-*",
    ]
  }
}
resource "aws_iam_policy" "submit_jobs" {
  name   = "${local.name}-submit-jobs"
  policy = data.aws_iam_policy_document.submit_jobs.json
  tags   = local.tags
}
resource "aws_iam_role_policy_attachment" "api_submit_jobs" {
  role       = aws_iam_role.ecs_task.name
  policy_arn = aws_iam_policy.submit_jobs.arn
}
resource "aws_iam_role_policy_attachment" "batch_job_submit_jobs" {
  for_each   = toset(["preprocess", "simulation"])
  role       = aws_iam_role.batch_job[each.key].name
  policy_arn = aws_iam_policy.submit_jobs.arn
}


# ------------------------------------------------------------ Alerting
# With alert_email set, AWS emails a confirmation link first.
resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
  tags = local.tags
}
resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ------------------------------------------------------------- Outputs
output "api_url" {
  description = "Deployed API base URL (the public ALB)."
  value       = "http://${aws_lb.api.dns_name}"
}
output "s3_bucket" {
  description = "Private versioned artifact bucket."
  value       = aws_s3_bucket.artifacts.bucket
}
output "rds_endpoint" {
  description = "RDS Postgres endpoint (private; run db/schema.sql against it once)."
  value       = aws_db_instance.quotes.address
}
output "ecr_repositories" {
  description = "ECR repository URLs, one per image."
  value = {
    api         = aws_ecr_repository.api.repository_url
    preprocess  = aws_ecr_repository.preprocess.repository_url
    simulation  = aws_ecr_repository.simulation.repository_url
    postprocess = aws_ecr_repository.postprocess.repository_url
  }
}
output "batch_job_queues" {
  description = "Per-stage Batch job queues."
  value = {
    preprocess  = aws_batch_job_queue.preprocess.arn
    simulation  = aws_batch_job_queue.simulation.arn
    postprocess = aws_batch_job_queue.postprocess.arn
  }
}
