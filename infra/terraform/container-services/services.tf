data "aws_region" "current" {}

locals {
  services_enabled = local.internal_lb && var.platform_image != "" && var.repo_intelligence_image != "" && var.temporal != null
  region           = data.aws_region.current.region
  runtime_volume   = "tmp"
  checkouts_path   = "/repositories"
  index_path       = "/app/repository_indexes"
  # The platform image's entrypoint writes environment_config here, under TMPDIR.
  env_config_dir = "/tmp/platform-config"

  # Each service names what it reaches. `database` injects the platform_app credentials,
  # `temporal` the Temporal client settings and group, `internal_client` the group that
  # reaches the internal load balancer, and `target` the service's place behind it.
  service_specs = {
    workflow-worker = {
      image           = var.platform_image
      command         = ["python", "-m", "services.orchestration.worker"]
      cpu             = 512
      memory          = 1024
      database        = true
      temporal        = true
      internal_client = false
      target          = ""
      mounts          = []
      secrets         = {}
      environment     = { WORKER_ROLE = "workflow", HEALTH_HEARTBEAT_FILE = "/tmp/worker.heartbeat" }
      health          = ["CMD", "python", "-m", "services.healthcheck", "heartbeat", "/tmp/worker.heartbeat", "--max-age", "60"]
    }
    review-worker = {
      image           = var.platform_image
      command         = ["python", "-m", "services.orchestration.worker"]
      cpu             = 1024
      memory          = 2048
      database        = true
      temporal        = true
      internal_client = true
      target          = ""
      mounts          = [{ volume = "checkouts", path = local.checkouts_path, read_only = true }]
      secrets = merge(
        { REVIEW_SIGNING_KEY = "review_signing_key", REVIEW_ATTESTATION_SECRET = "review_attestation_secret" },
        local.review_exports_to_langfuse ? { LANGFUSE_PUBLIC_KEY = "langfuse_public_key", LANGFUSE_SECRET_KEY = "langfuse_secret_key" } : {},
      )
      environment = {
        WORKER_ROLE                   = "review"
        HEALTH_HEARTBEAT_FILE         = "/tmp/worker.heartbeat"
        REBAR_REVIEW_NATIVE           = "true"
        REBAR_LLM_CONFIG_FILE         = "/app/config/platform/rebar-review.toml"
        EVIDENCE_OBJECT_STORE_ENABLED = var.evidence_bucket_arn != "" ? "true" : "false"
        EVIDENCE_OBJECT_STORE_BUCKET  = var.evidence_bucket_arn != "" ? element(split(":", var.evidence_bucket_arn), 5) : ""
        EVIDENCE_OBJECT_STORE_REGION  = local.region
        REVIEW_TRUST_ROOT_PATH        = contains(keys(var.environment_config), "review-trust-root.yaml") ? "${local.env_config_dir}/review-trust-root.yaml" : ""
        LANGFUSE_HOST                 = local.review_exports_to_langfuse ? "https://langfuse.${var.internal_domain}" : ""
      }
      health = ["CMD", "python", "-m", "services.healthcheck", "heartbeat", "/tmp/worker.heartbeat", "--max-age", "60"]
    }
    coordination = {
      image           = var.platform_image
      command         = ["python", "-m", "services.coordination.service"]
      cpu             = 512
      memory          = 1024
      database        = true
      temporal        = true
      internal_client = false
      target          = "coordination"
      mounts          = []
      secrets         = { COORDINATION_SHARED_SECRET = "coordination_shared_secret" }
      environment     = { SERVER_TLS = "true", BIND_HOST = "0.0.0.0" }
      health          = ["CMD", "python", "-m", "services.healthcheck", "http", "8130", "--tls"]
    }
    repo-intelligence = {
      image           = var.repo_intelligence_image
      command         = ["python", "-m", "services.repository_intelligence.server"]
      cpu             = 1024
      memory          = 2048
      database        = false
      temporal        = false
      internal_client = false
      target          = "repo-intelligence"
      mounts = [
        { volume = "checkouts", path = local.checkouts_path, read_only = true },
        { volume = "index", path = local.index_path, read_only = false },
      ]
      secrets     = {}
      environment = { SERVER_TLS = "true", BIND_HOST = "0.0.0.0" }
      health      = ["CMD", "python", "-m", "services.healthcheck", "http", "8120", "--tls"]
    }
    repo-index-worker = {
      image           = var.repo_intelligence_image
      command         = ["python", "-m", "services.repository_intelligence.indexing.worker"]
      cpu             = 1024
      memory          = 2048
      database        = false
      temporal        = false
      internal_client = false
      target          = ""
      mounts = [
        { volume = "checkouts", path = local.checkouts_path, read_only = true },
        { volume = "index", path = local.index_path, read_only = false },
      ]
      secrets     = {}
      environment = { HEALTH_HEARTBEAT_FILE = "/tmp/worker.heartbeat" }
      health      = ["CMD", "python", "-m", "services.healthcheck", "heartbeat", "/tmp/worker.heartbeat", "--max-age", "120"]
    }
  }

  # db-migrate and schedule-bootstrap run once per deploy from the management tier.
  task_specs = {
    db-migrate = {
      image           = var.platform_image
      command         = ["alembic", "upgrade", "head"]
      cpu             = 512
      memory          = 1024
      database        = true
      temporal        = false
      internal_client = false
      target          = ""
      mounts          = []
      secrets         = {}
      environment     = { DB_MIGRATE_IN_CONTAINER = "1" }
      health          = []
    }
    schedule-bootstrap = {
      image           = var.platform_image
      command         = ["sh", "-c", "python -m scripts.reap --schedule && python -m scripts.intake --schedule"]
      cpu             = 512
      memory          = 1024
      database        = true
      temporal        = true
      internal_client = false
      target          = ""
      mounts          = []
      secrets         = {}
      environment     = {}
      health          = []
    }
  }

  services = local.services_enabled ? local.service_specs : {}

  # A PINNED execution runs only on workers of the build that started it, so each Temporal
  # worker has a blue and a green service. inv platform-deploy moves the idle slot to a new
  # build and scales the old slot to zero once no execution is pinned to its build.
  versioned_workers = toset(["workflow-worker", "review-worker"])
  slots             = ["blue", "green"]
  service_instances = merge(
    { for name, spec in local.services : name => { workload = name, spec = spec } if !contains(local.versioned_workers, name) },
    {
      for pair in setproduct([for name in keys(local.services) : name if contains(local.versioned_workers, name)], local.slots) :
      "${pair[0]}-${pair[1]}" => { workload = pair[0], spec = local.services[pair[0]] }
    },
  )
  tasks = local.services_enabled ? local.task_specs : {}
  workloads = merge(
    { for name, spec in local.services : name => merge(spec, { one_shot = false }) },
    { for name, spec in local.tasks : name => merge(spec, { one_shot = true }) },
  )

  database_url = "postgresql+asyncpg://${var.database_host}:${var.database_port}/${var.database_name}"
  platform_environment = merge(
    {
      ENVIRONMENT                   = var.environment
      LOG_LEVEL                     = var.log_level
      TMPDIR                        = "/tmp"
      AWS_REGION                    = local.region
      AWS_USE_FIPS_ENDPOINT         = var.use_fips_endpoints ? "true" : "false"
      BEDROCK_CONNECTIVITY_PROVIDER = "direct"
      LITELLM_LOCAL_MODEL_COST_MAP  = "True"
      DATABASE_URL                  = local.database_url
      DATABASE_POOL_SIZE            = tostring(var.database_pool_size)
      DATABASE_POOL_MAX_OVERFLOW    = tostring(var.database_pool_max_overflow)
      PGSSLMODE                     = "verify-full"
      PGSSLROOTCERT                 = "/etc/ssl/rds-global-bundle.pem"
      COORDINATION_URL              = "https://coordination.${var.internal_domain}"
      REPO_INTELLIGENCE_URL         = "https://repo-intelligence.${var.internal_domain}"
      REPO_INTELLIGENCE_REPOS       = var.repo_intelligence_repos
      PLATFORM_CA_PEM               = var.platform_ca_pem
      PLATFORM_ENV_CONFIG           = length(var.environment_config) == 0 ? "" : jsonencode(var.environment_config)
      TEMPORAL_HOST                 = try(var.temporal.endpoint, "")
      TEMPORAL_NAMESPACE            = try(var.temporal.namespace, "")
      TEMPORAL_TLS_SERVER_NAME      = try(var.temporal.server_name, "")
      OTEL_EXPORTER_OTLP_ENDPOINT   = local.collector_enabled ? local.collector_url : ""
      OTEL_SDK_DISABLED             = local.collector_enabled || local.langfuse_enabled ? "false" : "true"
      OTEL_TRACES_EXPORTER          = local.collector_enabled ? "" : "none"
      OTEL_METRICS_EXPORTER         = local.collector_enabled ? "" : "none"
      OTEL_LOGS_EXPORTER            = local.collector_enabled ? "" : "none"
    },
    var.https_proxy == "" ? {} : {
      HTTPS_PROXY = var.https_proxy
      NO_PROXY    = join(",", compact([var.no_proxy, ".${var.internal_domain}", ".amazonaws.com", "169.254.170.2"]))
    },
  )

  ca_secret = try(var.temporal.ca_secret_arn, null)

  all_workloads = merge(local.workloads, local.langfuse_workloads, local.collector_workloads)

  # With the collector running, the review worker's generation spans reach Langfuse through it.
  # Its execution role stays a reader of the Langfuse keys, because a draining worker slot runs
  # an older revision that may still inject them, and the secret store admits only the roles
  # listed here. A task receives the keys only with LANGFUSE_HOST, because a tracing library
  # given the keys without a host sends to Langfuse Cloud.
  review_exports_to_langfuse = local.langfuse_enabled && !local.collector_enabled
  extra_secret_reads = {
    for name, keys in { review-worker = ["langfuse_public_key", "langfuse_secret_key"] } : name => keys if local.langfuse_enabled
  }
  extra_task_statements = merge(
    local.langfuse_task_statements,
    { for name in keys(local.collector_workloads) : name => local.collector_statements if length(local.collector_statements) > 0 },
  )
  # A service restarts after a rotation of any credential it reads, the platform_app login included.
  workload_restart_keys = {
    for name, spec in local.all_workloads : name => concat(
      local.workload_secret_names[name],
      spec.database && name != "db-migrate" ? ["database_platform_app"] : [],
    )
  }
  restart_secret_keys = distinct(concat(local.store_secret_keys, ["database_platform_app"]))
  restart_instances = concat(
    [for instance, service in aws_ecs_service.platform : { id = service.id, workload = local.service_instances[instance].workload }],
    [for name, service in aws_ecs_service.langfuse : { id = service.id, workload = name }],
    [for name, service in aws_ecs_service.collector : { id = service.id, workload = name }],
  )

  # Secrets the module owns are not in the secret store, so the store lists no readers for them.
  store_secret_keys = distinct(flatten([for name in keys(local.all_workloads) : [for key in local.workload_secret_names[name] : key if key != "clickhouse_tls"]]))

  # A task secret names a secret key, or a key and a JSON field as key:field.
  secret_arn_lookup = merge(var.secret_arns, local.langfuse_secret_arns)
  secret_references = distinct(flatten([for spec in values(local.all_workloads) : concat(values(spec.secrets), values(try(spec.setup_secrets, {})))]))
  secret_value_from = {
    for reference in local.secret_references : reference => (
      length(split(":", reference)) == 1
      ? lookup(local.secret_arn_lookup, reference, "")
      : "${lookup(local.secret_arn_lookup, split(":", reference)[0], "")}:${split(":", reference)[1]}::"
    )
  }
  secret_kms_keys = merge(
    { for key in keys(local.secret_arn_lookup) : key => var.secret_kms_key_arn },
    local.langfuse_secret_kms_keys,
  )

  workload_repositories = merge(
    { for name, spec in local.workloads : name => [spec.image == var.platform_image ? "platform" : "repo-intelligence"] },
    { for name, spec in local.langfuse_workloads : name => spec.repositories },
    { for name, spec in local.collector_workloads : name => spec.repositories },
  )
}

resource "terraform_data" "services_guard" {
  count = local.services_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition = (
        var.cluster_arn != "" && length(var.management_subnet_ids) > 0 && length(var.data_subnet_ids) > 0 &&
        var.endpoints_security_group_id != "" && var.database_host != "" && length(var.database_cidr_blocks) > 0 &&
        var.database_app_secret_arn != "" && var.database_migrator_secret_arn != "" && var.secret_kms_key_arn != "" &&
        alltrue([for key in ["coordination_shared_secret", "review_signing_key", "review_attestation_secret"] : lookup(var.secret_arns, key, "") != ""])
      )
      error_message = "The platform services need cluster_arn, the management and data subnets, endpoints_security_group_id, the database host, CIDRs, and the platform_app and platform_migrator secrets, secret_kms_key_arn, and the coordination_shared_secret, review_signing_key, and review_attestation_secret secret ARNs."
    }
  }
}

resource "aws_kms_key" "services" {
  count = local.services_enabled ? 1 : 0

  description         = "Encrypts the ${var.name_prefix} platform service logs and workspace file system."
  enable_key_rotation = true
  policy              = local.services_key_policy

  tags = var.tags
}

resource "aws_kms_alias" "services" {
  count = local.services_enabled ? 1 : 0

  name          = "alias/${var.name_prefix}-container-services"
  target_key_id = aws_kms_key.services[0].key_id
}

locals {
  services_key_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowCloudWatchLogsForTheServiceLogGroups"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = { "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/ecs/${var.name_prefix}-platform/*" }
        }
      },
    ]
  })

  log_settings = {
    for name in keys(local.all_workloads) : name => {
      logDriver = "awslogs"
      options = {
        awslogs-group         = "/aws/ecs/${var.name_prefix}-platform/${name}"
        awslogs-region        = local.region
        awslogs-stream-prefix = name
      }
    }
  }

  workload_secrets = {
    for name, spec in local.all_workloads : name => concat(
      spec.database ? [
        { name = "PGUSER", valueFrom = "${name == "db-migrate" ? var.database_migrator_secret_arn : var.database_app_secret_arn}:username::" },
        { name = "PGPASSWORD", valueFrom = "${name == "db-migrate" ? var.database_migrator_secret_arn : var.database_app_secret_arn}:password::" },
      ] : [],
      spec.temporal && local.ca_secret != null ? [{ name = "TEMPORAL_TLS_CA", valueFrom = local.ca_secret }] : [],
      [for variable, reference in spec.secrets : { name = variable, valueFrom = local.secret_value_from[reference] }],
    )
  }

  workload_environment = {
    for name, spec in local.workloads : name => [
      for key, value in merge(
        spec.image == var.platform_image ? local.platform_environment : {
          LOG_LEVEL               = var.log_level
          TMPDIR                  = "/tmp"
          REPO_INTELLIGENCE_REPOS = var.repo_intelligence_repos
        },
        spec.environment,
        lookup(var.service_environment, name, {}),
      ) : { name = key, value = value } if value != ""
    ]
  }

  volume_permissions = {
    for name, spec in local.workloads : name => {
      name                   = "volume-permissions"
      image                  = spec.image
      essential              = false
      user                   = "0"
      readonlyRootFilesystem = true
      entryPoint             = ["/bin/chmod"]
      command                = ["1777", "/tmp"]
      mountPoints            = [{ sourceVolume = local.runtime_volume, containerPath = "/tmp" }]
      logConfiguration       = local.log_settings[name]
    }
  }
}

resource "aws_cloudwatch_log_group" "workload" {
  for_each = local.all_workloads

  name              = "/aws/ecs/${var.name_prefix}-platform/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.services[0].arn

  tags = var.tags
}

resource "aws_ecs_task_definition" "workload" {
  for_each = local.workloads

  # A service keeps running the revision inv platform-deploy last moved it to, so a superseded
  # revision stays active.
  skip_destroy = true

  family                   = "${var.name_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.workload_execution[each.key].arn
  task_role_arn            = aws_iam_role.workload_task[each.key].arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = local.runtime_volume
  }

  dynamic "volume" {
    for_each = { for mount in each.value.mounts : mount.volume => mount }

    content {
      name = volume.key

      efs_volume_configuration {
        file_system_id     = aws_efs_file_system.workspace[0].id
        transit_encryption = "ENABLED"

        authorization_config {
          access_point_id = aws_efs_access_point.workspace[volume.key].id
          iam             = "ENABLED"
        }
      }
    }
  }

  container_definitions = jsonencode([
    local.volume_permissions[each.key],
    merge(
      {
        name                   = each.key
        dependsOn              = [{ containerName = "volume-permissions", condition = "SUCCESS" }]
        image                  = each.value.image
        essential              = true
        command                = each.value.command
        readonlyRootFilesystem = true
        environment            = local.workload_environment[each.key]
        secrets                = local.workload_secrets[each.key]
        mountPoints = concat(
          [{ sourceVolume = local.runtime_volume, containerPath = "/tmp" }],
          [for mount in each.value.mounts : { sourceVolume = mount.volume, containerPath = mount.path, readOnly = mount.read_only }],
        )
        portMappings     = each.value.target == "" ? [] : [{ containerPort = local.internal_services[each.value.target].port, protocol = "tcp" }]
        logConfiguration = local.log_settings[each.key]
      },
      length(each.value.health) == 0 ? {} : {
        healthCheck = {
          command     = each.value.health
          interval    = 30
          timeout     = 10
          retries     = 3
          startPeriod = 180
        }
      },
    ),
  ])

  tags = var.tags

  depends_on = [aws_cloudwatch_log_group.workload]
}

resource "aws_ecs_service" "platform" {
  for_each = local.service_instances

  name                              = "${var.name_prefix}-${each.key}"
  cluster                           = var.cluster_arn
  task_definition                   = aws_ecs_task_definition.workload[each.value.workload].arn
  desired_count                     = 0
  enable_execute_command            = var.enable_execute_command
  health_check_grace_period_seconds = each.value.spec.target == "" ? null : 180
  propagate_tags                    = "SERVICE"

  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets         = var.application_subnet_ids
    security_groups = local.workload_security_groups[each.value.workload]
  }

  dynamic "load_balancer" {
    for_each = each.value.spec.target == "" ? [] : [each.value.spec.target]

    content {
      target_group_arn = aws_lb_target_group.internal[load_balancer.value].arn
      container_name   = each.value.workload
      container_port   = local.internal_services[load_balancer.value].port
    }
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = var.tags

  # inv platform-deploy migrates the database before it moves a service to a new task definition
  # and sets the task count, and the off-hours schedule scales the count.
  lifecycle {
    ignore_changes = [desired_count, task_definition]
  }

  depends_on = [aws_lb_listener_rule.internal]
}
