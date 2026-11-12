# dns-firewall

Reusable module that filters the VPC's DNS through a Route 53 Resolver DNS Firewall allowlist. A query for a listed domain resolves, and every other query is answered NXDOMAIN, so a workload can reach only the destinations the platform names. This is the deny-by-default layer for outbound traffic, alongside the security groups and the single egress path from the [egress](../egress) module. Nothing is created unless `create_dns_firewall = true`.

## What it provisions

- An allowlist domain list and a catch-all `*` domain list.
- A rule group that allows the allowlist at priority 100 and applies `default_action` to everything else at priority 200, associated with the VPC.
- A firewall configuration that fails closed, so queries are blocked rather than resolved if the firewall is unavailable.
- Resolver query logging to a CloudWatch log group with `query_log_retention_days` retention, which records every query and the rule that matched it.

## Allowlist

The defaults cover the AWS service names under `amazonaws.com` that the endpoints, RDS, ElastiCache, ECR, and Systems Manager resolve, the EC2 internal names, GitHub, Atlassian, and the Slack webhook host. Add the SMTP relay and any private service discovery namespace through `additional_allowed_domains`. A `*.` entry matches subdomains only, so list an apex separately when it is also needed.

## Rollout

Set `default_action = "ALERT"` to resolve every query while logging the ones outside the allowlist. Review the query log for legitimate destinations, add them, then switch to `BLOCK`.

## Limits

DNS filtering stops a workload that resolves a name before it connects, which covers the platform's clients. It does not stop a client that connects to a literal IP address. AWS Network Firewall with a TLS SNI allowlist closes that gap where it is required.

## Usage

```hcl
module "dns_firewall" {
  source = "../dns-firewall"

  create_dns_firewall        = true
  name_prefix                = "agentic-platform"
  vpc_id                     = aws_vpc.this.id
  additional_allowed_domains = ["smtp.example.gov"]
  tags                       = var.tags
}
```
