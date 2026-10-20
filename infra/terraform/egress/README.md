# egress

Reusable module that gives the private application and management tiers an outbound internet path for GitHub, Atlassian, Slack, and SMTP. `egress_mode` selects the path, so a deployment starts on a low-cost NAT instance and moves to a managed NAT Gateway by changing one variable. The data tier never receives a route through it.

## Modes

| Mode | What it provisions | Use |
|---|---|---|
| `none` | Nothing | A network whose organization already provides egress, such as a CMS VPC routed through the enterprise proxy. Route the platform through it with `HTTPS_PROXY`. |
| `nat_instance` | An Amazon Linux 2023 NAT instance with an Elastic IP and default routes through its interface | Evaluation and testing, at a fraction of the NAT Gateway hourly and per-GB cost |
| `nat_gateway` | A NAT Gateway with an Elastic IP and default routes through it | Production, with AWS-managed availability and bandwidth |

Switching modes replaces the routes in one apply. The Elastic IP is kept, so a destination that allowlists the source address keeps working.

## NAT instance hardening

The instance becomes a host the deploying organization operates inside the boundary, so it is configured to the platform baseline:

- Amazon Linux 2023 minimal for arm64, resolved from the public SSM parameter. `ami` changes are ignored, so a new image is rolled out deliberately with `terraform apply -replace=module.egress.aws_instance.nat[0]`.
- IMDSv2 required with a hop limit of 1, an encrypted gp3 root volume, and no SSH key. Administration is through Systems Manager Session Manager with the `AmazonSSMManagedInstanceCore` policy.
- Security updates applied at boot with `dnf upgrade --security`. Ongoing patching follows the organization's Systems Manager patch baseline.
- NAT configured with the iptables steps in the AWS VPC user guide. Translation applies only to the `source_cidr_blocks`, and the security group forwards only `allowed_egress_ports`.
- EC2 automatic recovery restarts the instance on another host after a hardware failure. Losing the instance itself takes an apply to replace it.

## Filtering

This module provides the path. Destination filtering is the job of the [dns-firewall](../dns-firewall) module, which answers NXDOMAIN for every domain outside its allowlist. AWS Network Firewall with a TLS SNI allowlist is the production upgrade when filtering must also stop a client that connects by IP address.

## Usage

```hcl
module "egress" {
  source = "../egress"

  egress_mode             = "nat_instance"
  name_prefix             = "agentic-platform"
  vpc_id                  = aws_vpc.this.id
  public_subnet_id        = aws_subnet.public[0].id
  private_route_table_ids = [aws_route_table.app.id, aws_route_table.management.id]
  source_cidr_blocks      = ["10.0.1.0/24", "10.0.21.0/24"]
  tags                    = var.tags
}
```
