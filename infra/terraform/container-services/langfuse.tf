locals {
  langfuse_enabled = local.services_enabled && var.langfuse != null
  lf = var.langfuse != null ? var.langfuse : {
    web_image          = ""
    worker_image       = ""
    clickhouse_image   = ""
    public_url         = ""
    admin_email        = ""
    cache              = { endpoint = "", port = 6379, username = "", security_group_id = "" }
    clickhouse_storage = { file_system_id = "", file_system_arn = "", access_point_id = "", access_point_arn = "", security_group_id = "" }
    bucket             = { name = "", arn = "", kms_key_arn = "" }
  }

  clickhouse_namespace  = var.clickhouse_namespace != "" ? var.clickhouse_namespace : "data.${var.internal_domain}"
  clickhouse_host       = "clickhouse.${local.clickhouse_namespace}"
  clickhouse_ports      = { https = 8443, native = 9440 }
  clickhouse_user       = "clickhouse"
  clickhouse_credential = "clickhouse_password"
  clickhouse_uid        = 101

  # ClickHouse listens only over TLS. The loopback health check verifies the certificate, which
  # inv clickhouse-certs issues for the Cloud Map name and 127.0.0.1.
  clickhouse_config = <<-XML
    <clickhouse>
      <logger>
        <level>information</level>
        <console>1</console>
        <log remove="1"/>
        <errorlog remove="1"/>
      </logger>
      <listen_host>0.0.0.0</listen_host>
      <http_port remove="1"/>
      <tcp_port remove="1"/>
      <mysql_port remove="1"/>
      <postgresql_port remove="1"/>
      <interserver_http_port remove="1"/>
      <https_port>${local.clickhouse_ports.https}</https_port>
      <tcp_port_secure>${local.clickhouse_ports.native}</tcp_port_secure>
      <openSSL>
        <server>
          <certificateFile>/clickhouse-tls/server.pem</certificateFile>
          <privateKeyFile>/clickhouse-tls/server-key.pem</privateKeyFile>
          <verificationMode>none</verificationMode>
          <loadDefaultCAFile>false</loadDefaultCAFile>
          <disableProtocols>sslv2,sslv3,tlsv1,tlsv1_1</disableProtocols>
          <preferServerCiphers>true</preferServerCiphers>
        </server>
      </openSSL>
    </clickhouse>
  XML

  langfuse_environment = local.langfuse_enabled ? {
    HOSTNAME                                    = "0.0.0.0"
    TELEMETRY_ENABLED                           = "false"
    AWS_REGION                                  = local.region
    AWS_USE_FIPS_ENDPOINT                       = var.use_fips_endpoints ? "true" : "false"
    NODE_EXTRA_CA_CERTS                         = "/trust/platform-ca.pem"
    SSL_CERT_FILE                               = "/trust/ca-bundle.pem"
    DATABASE_HOST                               = var.database_host
    DATABASE_PORT                               = tostring(var.database_port)
    DATABASE_NAME                               = "langfuse"
    DATABASE_ARGS                               = "sslmode=require&sslaccept=strict"
    CLICKHOUSE_URL                              = "https://${local.clickhouse_host}:${local.clickhouse_ports.https}"
    CLICKHOUSE_MIGRATION_URL                    = "clickhouse://${local.clickhouse_host}:${local.clickhouse_ports.native}"
    CLICKHOUSE_MIGRATION_SSL                    = "true"
    CLICKHOUSE_USER                             = local.clickhouse_user
    CLICKHOUSE_DB                               = "default"
    CLICKHOUSE_CLUSTER_ENABLED                  = "false"
    LANGFUSE_AUTO_CLICKHOUSE_MIGRATION_DISABLED = "true"
    REDIS_HOST                                  = local.lf.cache.endpoint
    REDIS_PORT                                  = tostring(local.lf.cache.port)
    REDIS_USERNAME                              = local.lf.cache.username
    REDIS_TLS_ENABLED                           = "true"
    LANGFUSE_S3_EVENT_UPLOAD_BUCKET             = local.lf.bucket.name
    LANGFUSE_S3_EVENT_UPLOAD_REGION             = local.region
    LANGFUSE_S3_EVENT_UPLOAD_PREFIX             = "events/"
    LANGFUSE_S3_MEDIA_UPLOAD_BUCKET             = local.lf.bucket.name
    LANGFUSE_S3_MEDIA_UPLOAD_REGION             = local.region
    LANGFUSE_S3_MEDIA_UPLOAD_PREFIX             = "media/"
  } : {}

  langfuse_secrets = {
    DATABASE_USERNAME   = "database_langfuse:username"
    DATABASE_PASSWORD   = "database_langfuse:password"
    REDIS_AUTH          = "elasticache_user:password"
    CLICKHOUSE_PASSWORD = local.clickhouse_credential
    SALT                = "langfuse_salt"
    ENCRYPTION_KEY      = "langfuse_encryption_key"
  }

  langfuse_base = {
    database        = false
    temporal        = false
    internal_client = false
    mounts          = []
    setup_secrets   = {}
  }

  langfuse_specs = {
    clickhouse = merge(local.langfuse_base, {
      repositories  = ["clickhouse", "platform"]
      image         = local.lf.clickhouse_image
      target        = ""
      one_shot      = false
      secrets       = { CLICKHOUSE_PASSWORD = local.clickhouse_credential }
      setup_secrets = { CLICKHOUSE_TLS_CERTIFICATE = "clickhouse_tls:certificate", CLICKHOUSE_TLS_KEY = "clickhouse_tls:private_key" }
      environment   = { CLICKHOUSE_USER = local.clickhouse_user }
      health        = ["CMD", "wget", "-q", "--spider", "--ca-certificate=/clickhouse-tls/ca.pem", "https://127.0.0.1:${local.clickhouse_ports.https}/ping"]
    })
    langfuse-web = merge(local.langfuse_base, {
      repositories = ["langfuse-web", "platform", "tls-sidecar"]
      image        = local.lf.web_image
      target       = "langfuse"
      one_shot     = false
      secrets = merge(local.langfuse_secrets, {
        NEXTAUTH_SECRET                  = "langfuse_nextauth_secret"
        LANGFUSE_INIT_PROJECT_PUBLIC_KEY = "langfuse_public_key"
        LANGFUSE_INIT_PROJECT_SECRET_KEY = "langfuse_secret_key"
        LANGFUSE_INIT_USER_PASSWORD      = "langfuse_admin_password"
      })
      environment = merge(local.langfuse_environment, {
        NEXTAUTH_URL               = local.lf.public_url
        AUTH_DISABLE_SIGNUP        = "true"
        LANGFUSE_INIT_ORG_ID       = "agentic-dev-platform"
        LANGFUSE_INIT_ORG_NAME     = "Agentic Dev Platform"
        LANGFUSE_INIT_PROJECT_ID   = "agentic-dev-platform"
        LANGFUSE_INIT_PROJECT_NAME = "agentic-dev-platform"
        LANGFUSE_INIT_USER_EMAIL   = local.lf.admin_email
        LANGFUSE_INIT_USER_NAME    = "Administrator"
      })
      health = ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:3000/api/public/health"]
    })
    langfuse-worker = merge(local.langfuse_base, {
      repositories = ["langfuse-worker", "platform"]
      image        = local.lf.worker_image
      target       = ""
      one_shot     = false
      secrets      = local.langfuse_secrets
      environment  = local.langfuse_environment
      health       = ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:3030/api/health"]
    })
    # The Langfuse web entrypoint migrates ClickHouse with certificate verification off, so the
    # deploy runs the same migrations here with verification against the platform CA.
    clickhouse-migrate = merge(local.langfuse_base, {
      repositories = ["langfuse-web", "platform"]
      image        = local.lf.web_image
      target       = ""
      one_shot     = true
      secrets      = { CLICKHOUSE_PASSWORD = local.clickhouse_credential }
      environment  = { CLICKHOUSE_USER = local.clickhouse_user, SSL_CERT_FILE = "/trust/ca-bundle.pem" }
      health       = []
    })
  }
  langfuse_workloads = { for name, spec in local.langfuse_specs : name => spec if local.langfuse_enabled }

  langfuse_services = { for name, spec in local.langfuse_workloads : name => spec if !spec.one_shot }

  langfuse_secret_arns = {
    for key, arn in {
      elasticache_user = var.langfuse_cache_secret_arn
      clickhouse_tls   = try(aws_secretsmanager_secret.clickhouse_tls[0].arn, "")
    } : key => arn if local.langfuse_enabled
  }
  langfuse_secret_kms_keys = { for key, arn in { clickhouse_tls = try(aws_kms_key.services[0].arn, "") } : key => arn if local.langfuse_enabled }

  clickhouse_migrate_command = join(" ", [
    "cd /app/packages/shared &&",
    "exec migrate -source file://clickhouse/migrations/unclustered",
    "-database \"clickhouse://${local.clickhouse_host}:${local.clickhouse_ports.native}?username=$${CLICKHOUSE_USER}&password=$${CLICKHOUSE_PASSWORD}&database=default&x-multi-statement=true&secure=true&x-migrations-table-engine=MergeTree\"",
    "up",
  ])

  # The setup container runs as root from the platform image, which carries the RDS CA bundle,
  # and prepares the volumes the upstream images read with their read-only root file systems.
  # Prisma reads only the first certificate of an sslcert file, so it verifies Aurora through
  # SSL_CERT_FILE, a bundle of the system roots, the RDS roots, and the platform CA.
  trust_setup = "set -eu; printf '%s\\n' \"$PLATFORM_CA_PEM\" > /trust/platform-ca.pem; cat /etc/ssl/certs/ca-certificates.crt /etc/ssl/rds-global-bundle.pem /trust/platform-ca.pem > /trust/ca-bundle.pem; chmod 0444 /trust/*"
  setup_scripts = {
    clickhouse = join("; ", [
      "set -eu",
      "printf '%s' \"$CLICKHOUSE_PLATFORM_CONFIG\" > /clickhouse-config/platform.xml",
      "printf '%s\\n' \"$CLICKHOUSE_TLS_CERTIFICATE\" > /clickhouse-tls/server.pem",
      "printf '%s\\n' \"$CLICKHOUSE_TLS_KEY\" > /clickhouse-tls/server-key.pem",
      "printf '%s\\n' \"$PLATFORM_CA_PEM\" > /clickhouse-tls/ca.pem",
      "chown -R ${local.clickhouse_uid}:${local.clickhouse_uid} /clickhouse-config /clickhouse-users /clickhouse-tls",
      "chmod 0400 /clickhouse-tls/server-key.pem",
      "chmod 1777 /tmp-main",
    ])
    langfuse-web       = "${local.trust_setup}; chmod 1777 /tmp-main /tmp-sidecar"
    langfuse-worker    = "${local.trust_setup}; chmod 1777 /tmp-main"
    clickhouse-migrate = "${local.trust_setup}; chmod 1777 /tmp-main"
  }

  setup_volumes = {
    clickhouse         = ["clickhouse-config", "clickhouse-users", "clickhouse-tls", "tmp-main"]
    langfuse-web       = ["trust", "tmp-main", "tmp-sidecar"]
    langfuse-worker    = ["trust", "tmp-main"]
    clickhouse-migrate = ["trust", "tmp-main"]
  }

  main_mounts = {
    clickhouse = [
      { sourceVolume = "clickhouse-data", containerPath = "/var/lib/clickhouse", readOnly = false },
      { sourceVolume = "clickhouse-config", containerPath = "/etc/clickhouse-server/config.d", readOnly = true },
      { sourceVolume = "clickhouse-users", containerPath = "/etc/clickhouse-server/users.d", readOnly = false },
      { sourceVolume = "clickhouse-tls", containerPath = "/clickhouse-tls", readOnly = true },
      { sourceVolume = "tmp-main", containerPath = "/tmp", readOnly = false },
    ]
    langfuse-web = [
      { sourceVolume = "trust", containerPath = "/trust", readOnly = true },
      { sourceVolume = "tmp-main", containerPath = "/tmp", readOnly = false },
    ]
    langfuse-worker = [
      { sourceVolume = "trust", containerPath = "/trust", readOnly = true },
      { sourceVolume = "tmp-main", containerPath = "/tmp", readOnly = false },
    ]
    clickhouse-migrate = [
      { sourceVolume = "trust", containerPath = "/trust", readOnly = true },
      { sourceVolume = "tmp-main", containerPath = "/tmp", readOnly = false },
    ]
  }

  setup_secrets = {
    for name, spec in local.langfuse_workloads : name => [
      for variable, reference in spec.setup_secrets : { name = variable, valueFrom = local.secret_value_from[reference] }
    ]
  }

  langfuse_statements = {
    clickhouse = [
      {
        Sid      = "MountTheClickHouseData"
        Effect   = "Allow"
        Action   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
        Resource = local.lf.clickhouse_storage.file_system_arn
        Condition = {
          StringEquals = { "elasticfilesystem:AccessPointArn" = local.lf.clickhouse_storage.access_point_arn }
        }
      },
    ]
    langfuse-web    = local.langfuse_bucket_statements
    langfuse-worker = local.langfuse_bucket_statements
  }
  langfuse_task_statements = { for name, statements in local.langfuse_statements : name => statements if local.langfuse_enabled }

  langfuse_bucket_statements = concat(
    [
      {
        Sid      = "ReadAndWriteTheEventsAndMedia"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${local.lf.bucket.arn}/*"
      },
      {
        Sid      = "ListTheBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = local.lf.bucket.arn
      },
    ],
    local.lf.bucket.kms_key_arn == "" ? [] : [
      {
        Sid      = "EncryptTheObjects"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = local.lf.bucket.kms_key_arn
      },
    ],
  )

  clickhouse_clients = [for name in ["langfuse-web", "langfuse-worker", "clickhouse-migrate"] : name if local.langfuse_enabled]
  clickhouse_client_rules = {
    for pair in setproduct(local.clickhouse_clients, keys(local.clickhouse_ports)) : "${pair[0]}-${pair[1]}" => {
      client = pair[0]
      port   = local.clickhouse_ports[pair[1]]
    }
  }
  langfuse_database_egress = {
    for pair in setproduct([for name in ["langfuse-web", "langfuse-worker"] : name if local.langfuse_enabled], var.database_cidr_blocks) :
    "${pair[0]}-${pair[1]}" => { workload = pair[0], cidr = pair[1] }
  }
}

resource "terraform_data" "langfuse_guard" {
  count = local.langfuse_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition = (
        local.lf.admin_email != "" && local.lf.public_url != "" && var.platform_ca_pem != "" && var.langfuse_cache_secret_arn != "" && var.tls_sidecar_image != "" &&
        alltrue([for key in ["database_langfuse", "clickhouse_password", "langfuse_salt", "langfuse_encryption_key", "langfuse_nextauth_secret", "langfuse_public_key", "langfuse_secret_key", "langfuse_admin_password"] : lookup(var.secret_arns, key, "") != ""])
      )
      error_message = "Langfuse needs admin_email, public_url, platform_ca_pem, langfuse_cache_secret_arn, tls_sidecar_image, and the database_langfuse, clickhouse_password, and Langfuse secret store secrets."
    }
  }
}

resource "aws_service_discovery_private_dns_namespace" "data" {
  count = local.langfuse_enabled ? 1 : 0

  name        = local.clickhouse_namespace
  description = "Names the ${var.name_prefix} data services that run as ECS tasks."
  vpc         = var.vpc_id

  tags = var.tags
}

resource "aws_service_discovery_service" "clickhouse" {
  count = local.langfuse_enabled ? 1 : 0

  name = "clickhouse"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.data[0].id
    routing_policy = "MULTIVALUE"

    dns_records {
      ttl  = 10
      type = "A"
    }
  }

  tags = var.tags
}

resource "aws_secretsmanager_secret" "clickhouse_tls" {
  count = local.langfuse_enabled ? 1 : 0

  name                    = "${var.name_prefix}/clickhouse/tls"
  description             = "JSON with the PEM certificate and private_key ClickHouse presents, which inv clickhouse-certs put writes."
  kms_key_id              = aws_kms_key.services[0].arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(var.tags, { SecretGroup = "clickhouse-tls" })
}

resource "aws_secretsmanager_secret_policy" "clickhouse_tls" {
  count = local.langfuse_enabled ? 1 : 0

  secret_arn = aws_secretsmanager_secret.clickhouse_tls[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowTheClickHouseExecutionRole"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.workload_execution["clickhouse"].arn }
        Action    = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource  = "*"
      },
      {
        Sid       = "DenyOtherReaders"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = "*"
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = aws_iam_role.workload_execution["clickhouse"].arn } }
      },
    ]
  })
}

resource "aws_ecs_task_definition" "langfuse" {
  for_each = local.langfuse_workloads

  skip_destroy = true

  family                   = "${var.name_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = try(var.langfuse_task_sizes[each.key].cpu, 512)
  memory                   = try(var.langfuse_task_sizes[each.key].memory, 1024)
  execution_role_arn       = aws_iam_role.workload_execution[each.key].arn
  task_role_arn            = aws_iam_role.workload_task[each.key].arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  dynamic "volume" {
    for_each = toset(local.setup_volumes[each.key])

    content {
      name = volume.key
    }
  }

  dynamic "volume" {
    for_each = each.key == "clickhouse" ? [1] : []

    content {
      name = "clickhouse-data"

      efs_volume_configuration {
        file_system_id     = local.lf.clickhouse_storage.file_system_id
        transit_encryption = "ENABLED"

        authorization_config {
          access_point_id = local.lf.clickhouse_storage.access_point_id
          iam             = "ENABLED"
        }
      }
    }
  }

  container_definitions = jsonencode(concat(
    [
      {
        name                   = "setup"
        image                  = var.platform_image
        essential              = false
        user                   = "0"
        readonlyRootFilesystem = true
        entryPoint             = ["/bin/sh", "-c"]
        command                = [local.setup_scripts[each.key]]
        environment = concat(
          [{ name = "PLATFORM_CA_PEM", value = var.platform_ca_pem }],
          each.key == "clickhouse" ? [{ name = "CLICKHOUSE_PLATFORM_CONFIG", value = local.clickhouse_config }] : [],
        )
        secrets          = local.setup_secrets[each.key]
        mountPoints      = [for volume in local.setup_volumes[each.key] : { sourceVolume = volume, containerPath = "/${volume}" }]
        logConfiguration = local.log_settings[each.key]
      },
      merge(
        {
          name                   = each.key
          dependsOn              = [{ containerName = "setup", condition = "SUCCESS" }]
          image                  = each.value.image
          essential              = true
          user                   = each.key == "clickhouse" ? "${local.clickhouse_uid}:${local.clickhouse_uid}" : null
          readonlyRootFilesystem = true
          environment            = [for key, value in each.value.environment : { name = key, value = value } if value != ""]
          secrets                = local.workload_secrets[each.key]
          mountPoints            = local.main_mounts[each.key]
          portMappings = each.key == "clickhouse" ? [
            for port in values(local.clickhouse_ports) : { containerPort = port, protocol = "tcp" }
          ] : []
          logConfiguration = local.log_settings[each.key]
        },
        each.key == "clickhouse-migrate" ? {
          entryPoint = ["/bin/sh", "-c"]
          command    = [local.clickhouse_migrate_command]
        } : {},
        length(each.value.health) == 0 ? {} : {
          healthCheck = {
            command     = each.value.health
            interval    = 30
            timeout     = 10
            retries     = 3
            startPeriod = 300
          }
        },
      ),
    ],
    each.key != "langfuse-web" ? [] : [
      {
        name                   = "tls-sidecar"
        dependsOn              = [{ containerName = "setup", condition = "SUCCESS" }]
        image                  = var.tls_sidecar_image
        essential              = true
        readonlyRootFilesystem = true
        command                = ["proxy", "-listen", ":${local.internal_services.langfuse.port}", "-upstream", "http://127.0.0.1:3000", "-cert-dir", "/tmp/tls-sidecar"]
        environment = [
          { name = "HEALTHPROBE_CA_FILE", value = "/tmp/tls-sidecar/cert.pem" },
          { name = "HEALTHPROBE_SERVER_NAME", value = "localhost" },
        ]
        mountPoints      = [{ sourceVolume = "tmp-sidecar", containerPath = "/tmp" }]
        portMappings     = [{ containerPort = local.internal_services.langfuse.port, protocol = "tcp" }]
        logConfiguration = local.log_settings[each.key]
        healthCheck = {
          command     = ["CMD", "/usr/local/bin/healthprobe", "https://127.0.0.1:${local.internal_services.langfuse.port}/api/public/health"]
          interval    = 30
          timeout     = 10
          retries     = 3
          startPeriod = 300
        }
      },
    ],
  ))

  tags = var.tags

  depends_on = [aws_cloudwatch_log_group.workload]
}

resource "aws_ecs_service" "langfuse" {
  for_each = local.langfuse_services

  name                              = "${var.name_prefix}-${each.key}"
  cluster                           = var.cluster_arn
  task_definition                   = aws_ecs_task_definition.langfuse[each.key].arn
  desired_count                     = 0
  enable_execute_command            = var.enable_execute_command
  health_check_grace_period_seconds = each.value.target == "" ? null : 300
  propagate_tags                    = "SERVICE"

  # Two ClickHouse servers must never open the same data directory, so a deployment stops the
  # running task before it starts the next.
  deployment_minimum_healthy_percent = each.key == "clickhouse" ? 0 : 100
  deployment_maximum_percent         = each.key == "clickhouse" ? 100 : 200

  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets         = var.application_subnet_ids
    security_groups = local.workload_security_groups[each.key]
  }

  dynamic "load_balancer" {
    for_each = each.value.target == "" ? [] : [each.value.target]

    content {
      target_group_arn = aws_lb_target_group.internal[load_balancer.value].arn
      container_name   = "tls-sidecar"
      container_port   = local.internal_services[load_balancer.value].port
    }
  }

  dynamic "service_registries" {
    for_each = each.key == "clickhouse" ? [1] : []

    content {
      registry_arn = aws_service_discovery_service.clickhouse[0].arn
    }
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = var.tags

  lifecycle {
    ignore_changes = [desired_count, task_definition]
  }

  depends_on = [aws_lb_listener_rule.internal]
}

resource "aws_vpc_security_group_ingress_rule" "clickhouse_from_clients" {
  for_each = local.clickhouse_client_rules

  security_group_id            = aws_security_group.workload["clickhouse"].id
  description                  = "ClickHouse over TLS from ${each.value.client}"
  from_port                    = each.value.port
  to_port                      = each.value.port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.workload[each.value.client].id
}

resource "aws_vpc_security_group_egress_rule" "clients_to_clickhouse" {
  for_each = local.clickhouse_client_rules

  security_group_id            = aws_security_group.workload[each.value.client].id
  description                  = "ClickHouse over TLS"
  from_port                    = each.value.port
  to_port                      = each.value.port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.workload["clickhouse"].id
}

resource "aws_vpc_security_group_egress_rule" "clickhouse_to_storage" {
  count = local.langfuse_enabled ? 1 : 0

  security_group_id            = aws_security_group.workload["clickhouse"].id
  description                  = "NFS over TLS to the ClickHouse file system"
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
  referenced_security_group_id = local.lf.clickhouse_storage.security_group_id
}

resource "aws_vpc_security_group_egress_rule" "langfuse_to_database" {
  for_each = local.langfuse_database_egress

  security_group_id = aws_security_group.workload[each.value.workload].id
  description       = "PostgreSQL to the data tier"
  from_port         = var.database_port
  to_port           = var.database_port
  ip_protocol       = "tcp"
  cidr_ipv4         = each.value.cidr
}

resource "aws_vpc_security_group_egress_rule" "langfuse_to_cache" {
  for_each = toset([for name in ["langfuse-web", "langfuse-worker"] : name if local.langfuse_enabled])

  security_group_id            = aws_security_group.workload[each.key].id
  description                  = "Valkey over TLS to the cache"
  from_port                    = local.lf.cache.port
  to_port                      = local.lf.cache.port
  ip_protocol                  = "tcp"
  referenced_security_group_id = local.lf.cache.security_group_id
}
