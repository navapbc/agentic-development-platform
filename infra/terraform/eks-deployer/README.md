# eks-deployer

Reusable module that provisions the one-shot ECS task that deploys the EKS workloads from inside the VPC. A workstation starts it with `inv eks-deploy`, and the secret store's restart rule starts it after a credential rotation, so neither needs kubectl, helm, or a route to the private Kubernetes API. Nothing is created unless `create_eks_deployer = true`, and the task definition needs `image` as well.

## What it provisions

- A Fargate task definition, `<name_prefix>-eks-deployer`, for arm64, that runs the eks-deployer image with a read-only root file system and a task volume at `/tmp`. Its only environment variable, `DEPLOY_INPUTS`, holds the environment's images, settings, security groups, secret ARNs, and network ranges, and no secret value.
- An execution role that pulls only from `repository_arn` and writes only to the task log group.
- A task role with no AWS permission. The task signs its Kubernetes API token with it, and the cluster grants the role access through the access entry the composition root creates.
- A security group with no ingress and egress only to the VPC interface endpoints and S3 on 443. The `eks-cluster` module adds the egress to the API endpoint when the group is in its `api_access_security_group_ids`.
- A log group under a KMS key of its own, kept for `log_retention_days`, 365 by default.

## The image

`docker/eks-deployer.Dockerfile` builds the image that `inv publish-platform-images` and the `publish-images` workflow push to the `eks-deployer` repository of `container-services`. It holds kubectl, helm, and aws-iam-authenticator, each pinned by version and checksum per architecture, the AWS Load Balancer Controller chart, vendored because DNS Firewall resolves no chart repository, and the AWS overlay with `scripts/eks_render.py` and `scripts/eks_deploy_task.py`. It runs as `nobody`.

The task's commands are below.

- `deploy` installs or upgrades the controller from the regional EKS registry, restarts it onto its webhook certificate, renders and applies the overlay, waits for each deployment, sets the MCP gateway's host list once its load balancer exists, and lists the pods no NetworkPolicy selects. A deployment that does not roll out has its pods, warnings, and recent log printed.
- `restart DEPLOYMENT...` rolls the named deployments.
- `check-policies` lists the pods no NetworkPolicy selects and exits 1 when there are any.

## FIPS 140 and FedRAMP

The module supports AC-6 through a task role that holds no AWS permission and an execution role limited to one repository and one log group, SC-7 through a group with no ingress and egress only to the endpoints, S3, and the API, CM-2 and CM-14 through tools pinned by checksum in an image the release workflow builds and attests, and AU-2 and AU-12 through the task log in a KMS-encrypted log group alongside the cluster's audit log.

## Tests

`tests/eks_deployer.tftest.hcl` plans the module against a mocked provider and asserts that the disabled module creates nothing, that no task is defined without an image, the task's architecture, read-only root, and inputs, the execution role's scope, the security group's egress, and the deploy output. Run it with `inv terraform-test`, which CI also runs.

## Outputs

| Output | Description |
|---|---|
| `task_role_arn`, `execution_role_arn` | The task's roles, for the access entry and the roles a runner passes |
| `security_group_id` | The task's group, for the API's admitted groups |
| `family`, `container` | Task family and container, for a run that takes the latest revision |
| `log_group_name` | The task log group |
| `deploy` | Cluster, subnets, task definition, container, group, and log group `inv eks-deploy` runs the task with. Null without the image |
