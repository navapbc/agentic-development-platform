locals {
  runtime_volume = "tmp"

  # Fargate creates a task volume owned by root, and the Temporal images run as an unprivileged
  # user with a read-only root filesystem. This container opens the volume to that user, the
  # way /tmp is, and exits before the main container starts.
  volume_permissions = {
    for name, image in { server = var.server_image, admin_tools = var.admin_tools_image, ui = var.ui_image } : name => {
      name                      = "volume-permissions"
      image                     = image
      essential                 = false
      user                      = "0"
      readonlyRootFilesystem    = true
      entryPoint                = ["/bin/chmod"]
      command                   = ["1777", "/tmp"]
      mountPoints               = [{ sourceVolume = local.runtime_volume, containerPath = "/tmp" }]
    }
  }
  after_volume_permissions = [{ containerName = "volume-permissions", condition = "SUCCESS" }]
  endpoint                 = local.deploy ? "${aws_lb.frontend[0].dns_name}:${local.frontend_port}" : ""

  # With an ACM frontend the server's own frontend listener, behind the NLB, presents the
  # internode certificate, so the server's internal frontend client verifies that name.
  server_frontend_name = local.frontend_from_secrets ? var.frontend_server_name : var.internode_server_name

  log_settings = {
    for name, role in local.roles : name => {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.task[role.log_group].name
        awslogs-region        = local.region
        awslogs-stream-prefix = role.log_group
      }
    }
  }

  fips_environment = var.fips_140_mode ? [{ name = "GODEBUG", value = "fips140=on" }] : []

  client_secrets = local.client_ca ? [
    { name = "TEMPORAL_TLS_CA_PEM", valueFrom = try(aws_secretsmanager_secret.tls["ca"].arn, "") },
  ] : []

  database_secrets = [
    { name = "POSTGRES_USER", valueFrom = "${var.database_secret_arn}:username::" },
    { name = "POSTGRES_PWD", valueFrom = "${var.database_secret_arn}:password::" },
  ]

  server_tls_secrets = concat(
    [
      { name = "TEMPORAL_TLS_CA_PEM", valueFrom = try(aws_secretsmanager_secret.tls["ca"].arn, "") },
      { name = "TEMPORAL_TLS_INTERNODE_CERT_PEM", valueFrom = "${try(aws_secretsmanager_secret.tls["internode"].arn, "")}:certificate::" },
      { name = "TEMPORAL_TLS_INTERNODE_KEY_PEM", valueFrom = "${try(aws_secretsmanager_secret.tls["internode"].arn, "")}:private_key::" },
    ],
    local.frontend_from_secrets ? [
      { name = "TEMPORAL_TLS_FRONTEND_CERT_PEM", valueFrom = "${try(aws_secretsmanager_secret.tls["frontend"].arn, "")}:certificate::" },
      { name = "TEMPORAL_TLS_FRONTEND_KEY_PEM", valueFrom = "${try(aws_secretsmanager_secret.tls["frontend"].arn, "")}:private_key::" },
    ] : [],
  )

  server_environment = concat(local.fips_environment, [
    { name = "DB", value = "postgres12" },
    { name = "DB_PORT", value = tostring(var.database_port) },
    { name = "POSTGRES_SEEDS", value = var.database_host },
    { name = "SQL_TLS_ENABLED", value = "true" },
    { name = "SQL_CA", value = "/etc/ssl/rds-global-bundle.pem" },
    { name = "SQL_HOST_VERIFICATION", value = "true" },
    { name = "SQL_HOST_NAME", value = var.database_host },
    { name = "NUM_HISTORY_SHARDS", value = tostring(var.num_history_shards) },
    { name = "BIND_ON_IP", value = "0.0.0.0" },
    { name = "TEMPORAL_RUNTIME_DIR", value = "/tmp/temporal-runtime" },
    { name = "TEMPORAL_DYNAMIC_CONFIG", value = length(var.dynamic_config) == 0 ? "{}" : yamlencode(var.dynamic_config) },
    { name = "TEMPORAL_TLS_INTERNODE_SERVER_NAME", value = var.internode_server_name },
    { name = "TEMPORAL_TLS_FRONTEND_SERVER_NAME", value = local.server_frontend_name },
    { name = "HEALTHPROBE_CA_FILE", value = "/tmp/temporal-runtime/ca.pem" },
    { name = "HEALTHPROBE_SERVER_NAME", value = local.server_frontend_name },
  ])

  admin_tools_environment = concat(local.fips_environment, [
    { name = "SQL_PLUGIN", value = "postgres12" },
    { name = "SQL_HOST", value = var.database_host },
    { name = "SQL_PORT", value = tostring(var.database_port) },
    { name = "SQL_TLS", value = "true" },
    { name = "SQL_TLS_CA_FILE", value = "/etc/ssl/rds-global-bundle.pem" },
    { name = "DBNAME", value = "temporal" },
    { name = "VISIBILITY_DBNAME", value = "temporal_visibility" },
    { name = "TEMPORAL_ADDRESS", value = local.endpoint },
    { name = "TEMPORAL_TLS_SERVER_NAME", value = var.frontend_server_name },
    { name = "DEFAULT_NAMESPACE", value = var.namespace },
    { name = "DEFAULT_NAMESPACE_RETENTION", value = var.namespace_retention },
    { name = "TMPDIR", value = "/tmp" },
  ])

  ui_environment = concat(local.fips_environment, [
    { name = "TEMPORAL_ADDRESS", value = local.endpoint },
    { name = "TEMPORAL_TLS_SERVER_NAME", value = var.frontend_server_name },
    { name = "TEMPORAL_TLS_ENABLE_HOST_VERIFICATION", value = "true" },
    { name = "TEMPORAL_UI_PORT", value = tostring(local.ui_port) },
    { name = "TEMPORAL_UI_RUNTIME_DIR", value = "/tmp/ui" },
    { name = "TEMPORAL_DEFAULT_NAMESPACE", value = var.namespace },
  ])
}

resource "aws_cloudwatch_log_group" "task" {
  for_each = toset(local.deploy ? concat(["server", "admin-tools"], local.ui ? ["ui"] : []) : [])

  name              = "/aws/ecs/${var.name_prefix}-temporal/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.temporal[0].arn

  tags = var.tags
}

resource "aws_ecs_task_definition" "server" {
  count = local.deploy ? 1 : 0

  # The service keeps running the revision it was last deployed to until inv temporal-deploy
  # moves it, so a superseded revision stays active.
  skip_destroy = true

  family                   = "${var.name_prefix}-temporal-server"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.server_cpu
  memory                   = var.server_memory
  execution_role_arn       = aws_iam_role.execution["server"].arn
  task_role_arn            = try(aws_iam_role.task["server"].arn, null)

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = local.runtime_volume
  }

  container_definitions = jsonencode([
    merge(local.volume_permissions["server"], { logConfiguration = local.log_settings["server"] }),
    {
      name                   = "server"
      dependsOn              = local.after_volume_permissions
      image                  = var.server_image
      essential              = true
      entryPoint             = ["/etc/temporal/platform-start.sh"]
      readonlyRootFilesystem = true
      portMappings = [
        { containerPort = local.frontend_port, protocol = "tcp" },
      ]
      environment = local.server_environment
      secrets     = concat(local.database_secrets, local.server_tls_secrets)
      mountPoints = [{ sourceVolume = local.runtime_volume, containerPath = "/tmp" }]
      healthCheck = {
        command     = ["CMD", "/usr/local/bin/healthprobe", "https://127.0.0.1:7243/api/v1/namespaces/temporal-system"]
        interval    = 15
        timeout     = 10
        retries     = 10
        startPeriod = 120
      }
      logConfiguration = local.log_settings["server"]
    },
  ])

  tags = var.tags
}

resource "aws_ecs_task_definition" "admin_tools" {
  count = local.deploy ? 1 : 0

  # The service keeps running the revision it was last deployed to until inv temporal-deploy
  # moves it, so a superseded revision stays active.
  skip_destroy = true

  family                   = "${var.name_prefix}-temporal-admin-tools"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution["admin_tools"].arn
  task_role_arn            = try(aws_iam_role.task["admin_tools"].arn, null)

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = local.runtime_volume
  }

  container_definitions = jsonencode([
    merge(local.volume_permissions["admin_tools"], { logConfiguration = local.log_settings["admin_tools"] }),
    {
      name                   = "admin-tools"
      dependsOn              = local.after_volume_permissions
      image                  = var.admin_tools_image
      essential              = true
      readonlyRootFilesystem = true
      environment            = local.admin_tools_environment
      secrets = concat(
        [
          { name = "SQL_USER", valueFrom = "${var.database_secret_arn}:username::" },
          { name = "SQL_PASSWORD", valueFrom = "${var.database_secret_arn}:password::" },
        ],
        local.client_secrets,
      )
      mountPoints      = [{ sourceVolume = local.runtime_volume, containerPath = "/tmp" }]
      logConfiguration = local.log_settings["admin_tools"]
    },
  ])

  tags = var.tags
}

resource "aws_ecs_service" "server" {
  count = local.deploy ? 1 : 0

  name                              = "${var.name_prefix}-temporal-server"
  cluster                           = var.cluster_arn
  task_definition                   = aws_ecs_task_definition.server[0].arn
  desired_count                     = 0
  enable_execute_command            = var.enable_execute_command
  health_check_grace_period_seconds = 180
  propagate_tags                    = "SERVICE"

  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets         = var.application_subnet_ids
    security_groups = [aws_security_group.server[0].id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.frontend[0].arn
    container_name   = "server"
    container_port   = local.frontend_port
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = var.tags

  # inv temporal-deploy applies the schema before it moves the service to a new task
  # definition and sets the task count, and the off-hours schedule scales the count.
  lifecycle {
    ignore_changes = [desired_count, task_definition]
  }

  depends_on = [aws_lb_listener.frontend]
}
