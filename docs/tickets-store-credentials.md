# Rebar Ticket-Store Credentials

The rebar ticket store keeps its storage credentials isolated from the evidence and audit stores. Each tenant declares its store in its own `tickets.yaml`, `config/local/tenants/<tenant>/tickets.yaml` locally, copied from the `tickets.yaml.example` of the tenant template. The platform declares the buckets and roles every store must avoid in `tickets_isolation.yaml`, `config/local/tickets_isolation.yaml` locally, copied from `config/local/tickets_isolation.yaml.example`. In AWS the operator writes `tickets_isolation.yaml` into `container_config_dir` beside the other platform files.

## Fields

- `store.bucket` and `store.prefix` name the S3 location the store syncs to through `git-remote-s3`. The bucket must differ from the evidence and audit buckets.
- `store.reader_profile` and `store.writer_profile` name the AWS profiles in the store's git remote URLs. Git fetches under the reader profile and pushes under the writer profile. The two must differ, and both are required when the store syncs to S3.
- `roles.read_only` is the role the reader profile assumes for the change-detection poll and every fetch. It has read-only access to the store.
- `roles.write` is the role the writer profile assumes for status transitions, linkback, approval recording, and every push. It differs from the read-only role.
- `provenance.require_actor` and `provenance.require_run_id` must both be true. Every tickets-branch ref write records the actor and the run id.
- `evidence_bucket`, `audit_bucket`, `evidence_writer_role`, and `audit_writer_role` of the platform's `tickets_isolation.yaml` name the buckets and roles no tenant's tickets store may reuse. A tenant `tickets.yaml` that sets `isolation` is refused.

## Remote

`inv rebar-store-remote` sets the store's git remote from these fields. The fetch URL is `s3://<reader_profile>@<bucket>/<prefix>` and the push URL is `s3://<writer_profile>@<bucket>/<prefix>`. The [rebar-ticket-store](../infra/terraform/rebar-ticket-store) module provisions the bucket and the two roles and shows the profile definitions.

## Validation

When the store's remote is an S3 URL, the activity worker validates the tenant's `tickets.yaml` against `tickets_isolation.yaml` at startup and refuses to start when any of these hold.

- The read-only polling role equals the write role, or the reader profile equals the writer profile.
- Either tickets role equals the evidence writer role or the audit writer role.
- The store bucket equals the evidence bucket or the audit bucket.
- Provenance is not required on writes.
- The store's fetch URL or push URL differs from the URL these fields produce.
- A profile is not defined, or it assumes a role other than the one this file names.

A store with no remote, or a remote that is not an S3 URL, is local-only, and the worker skips the check.
