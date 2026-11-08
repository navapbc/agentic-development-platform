# eks-cluster

Reusable module that provisions the EKS cluster for the workloads that need the Kubernetes API: the activity worker, which creates the OpenHands sandbox pods and the gate, checks, and scanner pods, LiteLLM, the inbound webhook, and the MCP gateway. Nothing is created unless `create_eks_cluster = true`.

## What it provisions

- A cluster named `<name_prefix>-eks`, or `cluster_name`, at `kubernetes_version`, 1.37 by default, on standard support only. The API endpoint is private, so a client reaches it from inside the VPC, and `api_access_security_group_ids` admits operator security groups on 443 and adds the matching egress rule to each.
- Envelope encryption of the Kubernetes secrets under a secrets key, and the `api`, `audit`, and `authenticator` control plane logs in the `/aws/eks/<cluster>/cluster` log group under a logs key, kept for `log_retention_days`, 365 by default.
- Access through EKS access entries only. The `API` authentication mode ignores the `aws-auth` ConfigMap, and the principal that creates the cluster receives no access. Each principal in `admin_principal_arns` receives `AmazonEKSClusterAdminPolicy` at cluster scope.
- Two managed node groups on Amazon Linux 2023 for arm64, which match the published images. The `platform` group runs the platform workloads and the add-ons. The `sandbox` group carries the `agentic-dev-platform/sandbox=true:NoSchedule` taint, so only pods that tolerate it run there. Both carry the `agentic-dev-platform/node-pool` label, require IMDSv2 at a hop limit of 1, and encrypt their root volumes under an EBS key. Each group scales between the sizes its variable sets. The module installs no autoscaler.
- Security groups for pods, through `ENABLE_POD_ENI` on the VPC CNI and the `AmazonEKSVPCResourceController` policy on the cluster role, in the `standard` enforcing mode, which keeps NetworkPolicy and Pod Identity working for a pod with its own security group. A SecurityGroupPolicy gives a pod a security group that a data store or a load balancer admits.
- The VPC CNI, kube-proxy, CoreDNS, the EKS Pod Identity agent, the EFS CSI driver, and the Secrets Store CSI driver with the AWS provider as managed add-ons. EKS installs no self-managed add-on. An add-on not listed in `addon_versions` receives the default version EKS publishes for the cluster version.
- Pod Identity roles for the service account of each entry in `workloads`, in the entry's `namespace` or else `platform_namespace`, by default `activity-worker` and `litellm`, and for the Load Balancer Controller, the VPC CNI, and the EFS CSI controller and node service accounts in `kube-system`. Each role trusts `pods.eks.amazonaws.com` for this cluster and only for the namespace and service account in the session tags EKS Pod Identity sets, so an association that binds the role to another service account cannot assume it. The `policy_arns` of each workload and `efs_csi_node_policy_arns` attach the caller's policies, such as the `bedrock:InvokeModel` policy of bedrock-iam to litellm.
- The IAM policy of the AWS Load Balancer Controller, vendored from the controller's `docs/install/iam_policy.json` at v3.6.2 with the partition templated. The module installs no Kubernetes object, so the [eks-deployer](../eks-deployer) task installs the controller chart with the settings in the `load_balancer_controller` output.

## Network policy

The VPC CNI enforces Kubernetes NetworkPolicy. In `standard` mode, the default, a new pod is open until the agent programs its policies. In `strict` mode every pod that is not host-networked starts denied and stays denied until a NetworkPolicy selects it. CoreDNS, the EFS CSI controller, and the Secrets Store CSI provider are such pods, and EKS reports an add-on ACTIVE only once its pods run, so a cluster created in strict mode fails its first apply. Create the cluster in `standard` mode, run `inv eks-deploy`, which applies the kube-system and platform NetworkPolicies and reports any pod no policy selects, then set `network_policy_enforcing_mode = "strict"` and apply again once it reports none. That apply changes only the VPC CNI configuration. A rebuild from empty state repeats the sequence.

## Instance metadata

The hop limit of 1 keeps every pod that is not host-networked from reaching the instance metadata service, so no pod reads the node role. A pod that needs AWS reads Pod Identity credentials, and a pod that needs the Region or the VPC receives them as settings. The Load Balancer Controller receives the `vpc_id` output and the Region as chart values for that reason.

## FIPS 140 and FedRAMP

The module supports SC-7 through the private API endpoint and the network policy enforcement, SC-28 and SC-12 through the customer-managed keys for the Kubernetes secrets, the node volumes, and the control plane logs, AU-2, AU-9, and AU-12 through the control plane audit log in an encrypted log group, AC-2, AC-3, and AC-6 through access entries and Pod Identity roles bound to one service account each, and CM-7 through IMDSv2 at a hop limit of 1. With `use_fips_endpoints`, the Secrets Store CSI provider calls the Secrets Manager FIPS endpoint. The KMS HSMs hold a FIPS 140-3 validation.

## Tests

`tests/eks_cluster.tftest.hcl` plans the module against a mocked provider and asserts the private endpoint, the secrets encryption, the control plane log types, the node launch templates and the sandbox taint, the EBS key policy, the Pod Identity trust and policies, the controller policy partition, and the network policy mode. Run it with `inv terraform-test`, which CI also runs.

## Usage

```hcl
module "eks_cluster" {
  source = "../eks-cluster"

  create_eks_cluster            = true
  name_prefix                   = "agentic-platform"
  subnet_ids                    = ["subnet-0123456789abcdef0", "subnet-0fedcba9876543210"]
  api_access_security_group_ids = [module.bedrock_jumphost.task_security_group_id]
  admin_principal_arns          = { operator = "arn:aws:iam::123456789012:role/platform-operator" }
  workloads = {
    activity_worker = { service_account = "activity-worker" }
    litellm         = { service_account = "litellm", policy_arns = [module.bedrock_iam.invoke_policy_arn] }
  }
  tags                          = var.tags
}
```

The map keys of `admin_principal_arns` and `workloads` and the lengths of the policy lists must be known at plan time. Key them by a static label and pass each list with a fixed number of elements, as above, so a plan from empty state succeeds.

## Outputs

| Output | Description |
|---|---|
| `cluster_name`, `cluster_arn` | Name and ARN of the cluster |
| `cluster_endpoint`, `cluster_certificate_authority_data` | Private API endpoint and its CA, for a kubeconfig |
| `cluster_security_group_id` | Security group of the control plane and the nodes, which a data store admits the pods through |
| `vpc_id` | VPC of the cluster, for the Load Balancer Controller |
| `service_cidr` | Service CIDR of the cluster, for the NetworkPolicies that reach the Kubernetes API Service |
| `node_role_arn` | Role of the managed nodes |
| `pod_identity_role_arns` | Pod Identity role of each workload, keyed by each workload name and by `load_balancer_controller`, `vpc_cni`, `efs_csi_controller`, and `efs_csi_node` |
| `secrets_kms_key_arn`, `ebs_kms_key_arn` | Keys of the Kubernetes secrets and the node volumes |
| `log_group_name` | Control plane log group |
| `node_pool_label`, `sandbox_taint` | Node label and sandbox taint for the pod specs |
| `load_balancer_controller` | Controller version, namespace, service account, and role for the chart install. Null when disabled |
