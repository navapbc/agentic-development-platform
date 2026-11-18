# ecs-cluster

Reusable module that provisions the ECS cluster the platform owns and shares. The Temporal control plane, the platform services, and the services other teams deploy run on it. Nothing is created unless `create_ecs_cluster = true`.

## What it provisions

- A cluster named `<name_prefix>-ecs`, or `cluster_name`, with the `FARGATE` and `FARGATE_SPOT` capacity providers and `FARGATE` as the default strategy. A service that names no capacity provider runs on `FARGATE`.
- Container Insights at the `enhanced` level by default. The module creates the `/aws/ecs/containerinsights/<cluster>/performance` log group before the cluster, under the logs key, because ECS otherwise creates it unencrypted on first use.
- ECS Exec sessions encrypted under the logs key, with `OVERRIDE` logging to the KMS-encrypted `/aws/ecs/<cluster>/exec` log group.
- Fargate ephemeral storage and managed storage encrypted under a storage key, whose policy admits `fargate.amazonaws.com` only for this cluster's encryption context and limits its grants to `Decrypt`.
- Both keys rotate yearly, and both log groups keep `log_retention_days`, 365 by default.
- A deployer policy, `<cluster>-deployer`, that a team attaches to its deploy role. It registers task definitions that run only on Fargate, creates, updates, and deletes services in this cluster, runs and stops tasks in this cluster, and lists only this cluster. It passes only the roles in `deployer_passable_role_arns`, and only to `ecs-tasks.amazonaws.com`. With that list empty it passes no role, so a task definition that names a task or execution role is refused.
- An exec task policy, `<cluster>-exec-task`, that a task role attaches to accept ECS Exec sessions and write their transcripts to the Exec log group.

## ECS Exec

A service accepts Exec sessions when it sets `enableExecuteCommand` and its task role carries the exec task policy. The operator who opens a session needs `ecs:ExecuteCommand` on the task and `kms:GenerateDataKey` on the logs key. The agent writes a transcript to the Exec log group only when the container image carries `script` and `cat`. On Alpine, `script` comes from `util-linux-misc`. Without it the session runs and no transcript is written.

## FIPS 140 and FedRAMP

The module supports SC-28 through the KMS encryption of Fargate ephemeral storage, SC-8 and SC-13 through the KMS-encrypted Exec data channel, AU-2, AU-9, and AU-12 through the encrypted Exec transcripts, AC-6 and AC-3 through the deployer policy confined to the cluster and to Fargate, and CA-7 through Container Insights. The KMS HSMs hold a FIPS 140-3 validation.

## Tests

`tests/ecs_cluster.tftest.hcl` plans the module against a mocked provider and asserts the encryption settings, the capacity providers, the key policy conditions, and that every deployer statement stays inside the cluster. Run it with `inv terraform-test`, which CI also runs.

## Usage

```hcl
module "ecs_cluster" {
  source = "../ecs-cluster"

  create_ecs_cluster          = true
  name_prefix                 = "agentic-platform"
  deployer_passable_role_arns = ["arn:aws:iam::123456789012:role/team-a-*"]
  tags                        = var.tags
}
```
