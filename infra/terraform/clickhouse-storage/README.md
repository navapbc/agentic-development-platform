# clickhouse-storage

Reusable module that provisions the persistent storage for the ClickHouse server that backs Langfuse. ClickHouse Cloud holds no FedRAMP authorization, so ClickHouse runs as a container-platform task inside the authorized account, and this module provides its encrypted file system in the data tier. Nothing is created unless `create_storage = true`.

## Placement

The file system and its mount targets live in the data-tier subnets. The ClickHouse task runs in the application tier, because it pulls its image and ships its logs through the VPC endpoints, and the data tier never originates connections. The task reaches the file system over NFS on port 2049, which the data-tier network ACL admits from the application tier.

## What it provisions

- An EFS file system with encryption at rest through the AWS managed key or `kms_key_arn`, and `elastic` throughput so ClickHouse merge bursts are not throttled.
- A mount target in each data-tier subnet behind a security group that admits NFS from `client_security_group_ids` and `client_cidr_blocks`, with no egress rule.
- An access point rooted at `/clickhouse` that enforces the `clickhouse` user and group, 101, the IDs of the official image.
- A file system policy that denies non-TLS access and denies any mount outside the access point.
- EFS automatic backups through AWS Backup.

## Task configuration

The ClickHouse task mounts the access point with transit encryption and IAM authorization enabled. Its task role needs `elasticfilesystem:ClientMount` and `elasticfilesystem:ClientWrite` on `file_system_arn`. Run the ClickHouse version pinned in `compose.yaml`, and confirm Langfuse compatibility before changing it. Langfuse 4.59.0 is verified against ClickHouse 26.9. Langfuse 4.15.0 fails every trace insert on ClickHouse 26.8 and 26.9 with a `DateTime64` range error.

## Usage

```hcl
module "clickhouse_storage" {
  source = "../clickhouse-storage"

  create_storage     = true
  name_prefix        = "agentic-platform"
  vpc_id             = aws_vpc.this.id
  subnet_ids         = aws_subnet.data[*].id
  client_cidr_blocks = ["10.0.1.0/24"]
  tags               = var.tags
}
```
