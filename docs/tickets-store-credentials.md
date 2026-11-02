# Rebar Ticket-Store Credentials

The rebar ticket store keeps its storage credentials isolated from the evidence and audit stores. The isolation is declared in `config/<env>/tickets.yaml`. Copy the committed template with `cp config/local/tickets.yaml.example config/local/tickets.yaml` and edit the copy.

## Fields

- `store.bucket` and `store.prefix` name the S3 location the store syncs to through `git-remote-s3`. The bucket must differ from the evidence and audit buckets.
- `store.reader_profile` and `store.writer_profile` name the AWS profiles in the store's git remote URLs. Git fetches under the reader profile and pushes under the writer profile. The two must differ, and both are required when the store syncs to S3.
- `roles.read_only` is the role the reader profile assumes for the change-detection poll and every fetch. It has read-only access to the store.
- `roles.write` is the role the writer profile assumes for status transitions, linkback, approval recording, and every push. It differs from the read-only role.
- `provenance.require_actor` and `provenance.require_run_id` must both be true. Every tickets-branch ref write records the actor and the run id.
- `isolation.evidence_bucket`, `isolation.audit_bucket`, `isolation.evidence_writer_role`, and `isolation.audit_writer_role` name the buckets and roles the tickets store must never reuse.

## Remote

`inv rebar-store-remote` sets the store's git remote from these fields. The fetch URL is `s3://<reader_profile>@<bucket>/<prefix>` and the push URL is `s3://<writer_profile>@<bucket>/<prefix>`. The [rebar-ticket-store](../infra/terraform/rebar-ticket-store) module provisions the bucket and the two roles and shows the profile definitions.

## Validation

When the store's remote is an S3 URL, the activity worker validates this file at startup and refuses to start when any of these hold.

- The read-only polling role equals the write role, or the reader profile equals the writer profile.
- Either tickets role equals the evidence writer role or the audit writer role.
- The store bucket equals the evidence bucket or the audit bucket.
- Provenance is not required on writes.
- The store's fetch URL or push URL differs from the URL these fields produce.
- A profile is not defined, or it assumes a role other than the one this file names.

A store with no remote, or a remote that is not an S3 URL, is local-only, and the worker skips the check.
