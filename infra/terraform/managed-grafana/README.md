# managed-grafana

Reusable module that provisions an Amazon Managed Grafana workspace over the collector's Prometheus workspace and X-Ray traces, and the read path to them. Nothing is created unless `create_managed_grafana = true`, and the [private-bedrock](../examples/private-bedrock) root sets it from its own `create_managed_grafana`, false by default.

## What it provisions

- A Grafana workspace, `<name_prefix>-grafana`, for the current account, with IAM Identity Center as its only sign-in, a customer managed permission type, and the `PROMETHEUS` and `XRAY` data source plugins enabled. Plugin installation from the Grafana UI is off. The workspace connects to `subnet_ids`, the application tier, so all of its outbound traffic, data sources included, flows through the VPC.
- An Identity Center group association per Grafana role, from `admin_group_ids`, `editor_group_ids`, and `viewer_group_ids`.
- A KMS key with rotation and the alias `alias/<name_prefix>-grafana`, which encrypts the workspace. AWS GovCloud (US) offers Amazon Managed Grafana no customer managed key, so in the `aws-us-gov` partition the module creates no key and the workspace uses an AWS owned key.
- The workspace role, `<name_prefix>-grafana`, which only Amazon Managed Grafana workspaces of this account in this region may assume. It holds `aps:QueryMetrics`, `aps:GetSeries`, `aps:GetLabels`, and `aps:GetMetricMetadata` on `prometheus_workspace_arn`, may read X-Ray traces and list regions for the X-Ray data source, and, when `xray_kms_key_arn` is set, may decrypt with that key. It assumes no other role.
- With `operator_principal_arns` set, the query role, `<name_prefix>-prometheus-query`, which only those principals may assume. It holds the same four query actions on `prometheus_workspace_arn` and nothing else, so it may query the workspace and may not write to it. With the list empty, the module creates no query role.

Neither role needs a grant on the Prometheus workspace's KMS key for a query, because Amazon Managed Service for Prometheus decrypts under its own grant on the key. The workspace role holds the query actions itself rather than assuming the query role, because the SigV4 signer in Grafana 10.4 builds its AWS session without regional STS, so a role assumption from the Prometheus data source goes to the global `sts.amazonaws.com` name in most commercial regions, which no interface endpoint serves and the workspace group does not admit.
- A security group for the workspace network interfaces with no ingress and egress only to `endpoints_security_group_id` on 443.

With `allowed_vpc_endpoint_ids` set, the workspace admits only those `grafana-workspace` interface endpoints, so operators reach it from inside the VPC. Empty admits any network, and Identity Center still authenticates every user.

The `interface_services` output lists the endpoints the workspace calls, `aps-workspaces`, `grafana-workspace`, and `xray`, which the caller adds to its [vpc-endpoints](../vpc-endpoints) module. The workspace calls the `xray` name, so the list holds it where the collector uses `xray-fips`. The module refuses a region without an Amazon Managed Grafana endpoint, and refuses `ap-northeast-2`, where the service cannot connect a workspace to a private VPC, so the `grafana-workspace` endpoint is added only where the region offers the workspace this module creates.

## Data sources

The module enables the Prometheus and X-Ray plugins through the workspace `data_sources` attribute and leaves the data source entries to a Grafana Admin. Provisioning them through the `grafana` Terraform provider would need a Grafana service account token in the provider configuration and in the state, and the module stores no Grafana credential. Each entry reads AWS with the workspace role, so neither holds a key. A Grafana Admin adds them once from the `data_sources` output:

1. Under Connections, add a Prometheus data source with the `prometheus.url` value as its URL. Turn on SigV4 auth, choose the workspace IAM role as the authentication provider, set the default region to `prometheus.region`, and leave the assume role ARN empty.
2. Under Apps, AWS Data Sources, choose X-Ray and the `xray.region` region, and add the data source. It reads with the workspace role.

## Prerequisites

- An IAM Identity Center instance, an organization instance or an account instance, that the account can use. The principal that applies the module needs the Identity Center permissions Amazon Managed Grafana uses to register the workspace application.
- The Identity Center group IDs for the Grafana roles. At least one admin group is required, because only a Grafana Admin adds the data sources.
- The collector exports of [container-services](../container-services), which create the Prometheus workspace, and the `aps-workspaces`, `grafana-workspace`, and `xray` interface endpoints with private DNS.
- Two application subnets in different Availability Zones, each with 15 free addresses.

## FIPS 140 and FedRAMP

The module supports CA-7 continuous monitoring and AU-6 audit review through the dashboards over the collector's metrics and traces. It supports AC-2 and IA-2 through Identity Center sign-in and group-based Grafana roles, AC-6 through a workspace role limited to four query actions on one workspace and trace reads, and an optional operator query role limited to the same four actions, SC-7 through VPC egress only to the interface endpoints and the optional restriction of inbound access to the `grafana-workspace` endpoint, SC-12 and SC-28 through the rotating customer managed key outside AWS GovCloud (US), and CM-7 through disabled plugin installation. Amazon Managed Grafana offers FIPS endpoints for its management API only in AWS GovCloud (US), and the workspace and its data source calls use the standard regional endpoints.

## Tests

`tests/managed_grafana.tftest.hcl` runs the module against a mocked provider and asserts that the disabled module creates nothing and needs no inputs, the workspace's Identity Center sign-in, data sources, encryption, and role associations, the VPC connection and its group's endpoint-only egress, that the workspace role holds the four query actions on the workspace, trace reads, and region listing and no role assumption, that the query role holds only the four query actions and no write action, trusts only the listed operators, and is absent without them, the trust conditions of the workspace role, the endpoint list without `sts`, the trace decryption key, the inbound endpoint restriction, the AWS owned key in AWS GovCloud (US), and the refusal of an unsupported region, of `ap-northeast-2`, and of missing inputs. Run it with `inv terraform-test`, which CI also runs.

## Outputs

| Output | Description |
|---|---|
| `workspace_id`, `workspace_url` | The workspace and the URL operators sign in at |
| `workspace_role_arn` | The workspace role |
| `query_role_arn` | The role operators assume to query the Prometheus workspace, which may not write to it. Null without `operator_principal_arns` |
| `security_group_id` | The group of the workspace network interfaces |
| `kms_key_arn` | The workspace key. Null in AWS GovCloud (US) |
| `data_sources` | The settings a Grafana Admin enters for the Prometheus and X-Ray data sources |
| `interface_services` | The interface endpoints the workspace calls, for the caller's vpc-endpoints module |

Every output is null, or empty for `interface_services`, when the module is disabled.
