# aurora-postgresql

Reusable module that provisions the platform's PostgreSQL database as an Aurora PostgreSQL cluster in the data tier. The defaults favor a low-cost evaluation stand with one Serverless v2 writer that pauses when idle. Production raises the instance count and capacity through variables. Nothing is created unless `create_cluster = true`.

## What it provisions

- An Aurora PostgreSQL 16 cluster in provisioned engine mode with Serverless v2 capacity, from `serverless_min_capacity` to `serverless_max_capacity` ACUs. A minimum of 0 pauses the cluster after `seconds_until_auto_pause` with no user connections and resumes it on the next connection.
- `instance_count` instances, one by default. Storage is replicated across three Availability Zones regardless. A second instance adds a reader that a failover promotes. With one instance, recovery recreates the writer, which is slower.
- Storage encryption with the AWS managed `aws/rds` key or `kms_key_arn`.
- A master user whose password RDS generates, stores in Secrets Manager, and rotates every seven days by default. The password never enters Terraform state.
- IAM database authentication enabled, for roles that connect with an `rds-db:connect` token.
- A DB subnet group over the data-tier subnets, which must span two Availability Zones.
- A security group that admits the database port from `ingress_security_group_ids` and `ingress_cidr_blocks` and has no egress rule, because the data tier never originates connections.
- A cluster parameter group that enforces TLS (`rds.force_ssl`), preloads `pg_stat_statements` and `pgaudit`, logs DDL through pgaudit and `log_statement`, and logs connections, disconnections, lock waits, temporary files, and errors.
- PostgreSQL log export to a CloudWatch log group with `log_retention_days` retention.
- Automated backups for `backup_retention_days`, deletion protection, a final snapshot on delete, and tags copied to snapshots.
- Instances on the `rds-ca-rsa2048-g1` certificate authority, with automatic minor version upgrades so back-ported security fixes apply in the maintenance window.

## After the first apply

The master user is for administration only. Each service connects as its own role with privileges scoped to its database. Creating those roles and databases, and running `CREATE EXTENSION pgaudit`, belongs to the role bootstrap, which runs from the management tier with the master secret. Services connect with `sslmode=verify-full` against the RDS CA bundle.

## Bedtime

`bedtime_tags` adds tags an organization scheduler such as Cloud Custodian reads, for example `CustodianRdsBedtime`. The platform's own off-hours schedule scales the services to zero so the cluster auto-pauses.

## Usage

```hcl
module "aurora_postgresql" {
  source = "../aurora-postgresql"

  create_cluster      = true
  name_prefix         = "agentic-platform"
  vpc_id              = aws_vpc.this.id
  subnet_ids          = aws_subnet.data[*].id
  ingress_cidr_blocks = ["10.0.1.0/24", "10.0.21.0/24"]
  tags                = var.tags
}
```
