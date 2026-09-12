# Rebar Ticket-Store Credentials

The rebar ticket store keeps its storage credentials isolated from the evidence and audit stores. The isolation is declared in `config/<env>/tickets.yaml` and validated at load time. Copy the committed template with `cp config/local/tickets.yaml.example config/local/tickets.yaml` and edit the copy.

## Fields

- `store.bucket` and `store.prefix` name the object-store location the rebar tickets branch syncs to. The bucket must differ from the evidence and audit buckets.
- `roles.read_only` is the role the change-detection poll assumes. It has read-only access to the tickets branch.
- `roles.write` is the role the write-back path assumes for status transitions, linkback, and approval recording. It differs from the read-only role.
- `provenance.require_actor` and `provenance.require_run_id` must both be true. Every tickets-branch ref write records the actor and the run id.
- `isolation.evidence_bucket`, `isolation.audit_bucket`, `isolation.evidence_writer_role`, and `isolation.audit_writer_role` name the buckets and roles the tickets store must never reuse.

## Validation

The loader fails closed when any of these hold.

- The read-only polling role equals the write role.
- Either tickets role equals the evidence writer role or the audit writer role.
- The store bucket equals the evidence bucket or the audit bucket.
- Provenance is not required on writes.

This unit declares and validates the isolation contract. It provisions no cloud resources. The buckets and roles named here are created and bound to IAM policies by the deployment that stands up the object store.
