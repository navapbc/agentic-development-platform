# Authored-identity coupling evaluation

This note records the evaluation that gates authored-identity signing. It establishes where rebar authorship state lives, whether the S3-backed ticket store detaches that state from the git structure, and the decision on whether implementation proceeds. It is grounded in the installed `nava-rebar` 0.12.0 authorship substrate.

## Where authorship state lives

An authoring identity is a rebar ticket of `ticket_type` `identity`. It carries a compiled `keys` list and an event-sourced `keyring`. `rebar.add_identity_key` and `rebar.revoke_identity_key` append `KEY_ADD` and `KEY_REVOKE` events to that ticket in the tracker git store, so the keyring is a position-scoped history rather than a mutable field. The first key on a keyless identity is admitted trust-on-first-use with no signature. A later add or a revoke requires a DSSE envelope over `authorship.keyop_payload` that verifies against a currently-valid key.

An authorship attestation is a DSSE envelope over an in-toto statement, signed with SSHSIG under the pinned `rebar.authorship.v1` namespace, with the author identity id as the signature principal. Verification resolves a trust root from the identity ticket. `rebar.attest.authorship.resolve_trust_root` renders an `allowed_signers` blob from the identity's current keys. `rebar.attest.authorship.verify_authorship_at_commit` builds an era-scoped trust root from exactly the keyring records that were live as of the event commit, deciding validity with `git merge-base --is-ancestor` against the tracker git history. Both verifiers fail closed, returning a non-verified verdict on any lookup or git error rather than raising.

The controlling fact is that key validity and authorship verification are decided by commit ancestry within the tracker git repository. Authorship state is git-anchored.

## Effect of the S3-backed store

The v0.1.0 rebar backend keeps the ticket store on a dedicated git orphan branch and syncs that branch to a private S3 bucket through `git-remote-s3`. That sync is transport of the same git branch to a bucket. It does not replace the commit history that authorship verification depends on, and it does not move authorship state out of the git structure. The object store therefore does not detach authorship from the git store. Authorship remains git-anchored whether or not the S3 option is enabled.

## Isolation

The v0.1.0 rebar backend keeps the store isolated on an orphan branch that is never pushed to a shared host. The identity tickets, the keyrings, and the authorship events live only in that isolated branch, so signing an authorship claim on the rebar backend does not expose identity or key material on a shared host.

## Verification model

The platform verifies authorship with the current-keys model. `verify_authorship_record` decodes the DSSE envelope and calls `rebar.attest.authorship.verify_event_authorship`, which binds the event uuid and the event content hash to the statement subject and then delegates signature verification to `verify_authorship`. `verify_authorship` resolves the trust root from the author identity's currently-valid keys.

The commit-era model in `verify_authorship_at_commit` is not used for platform authorship, and this is deliberate. The commit-era model resolves each keyring position and the event commit against the tracker git history with `git merge-base --is-ancestor`. It assumes the event commit is a commit in the same tracker repository that holds the keyring. Platform authorship events bind a commit in the target code repository being changed, not a commit in the rebar tracker repository. That commit is absent from the tracker history, so the ancestry check would resolve to nothing and the commit-era verifier would fail closed on every platform authorship event.

The commit remains cryptographically bound under the current-keys model. `build_authorship_event` records the commit in the event, `authorship_content_hash` covers the whole event, and `verify_event_authorship` rejects any statement whose subject digest does not match that content hash. A signature that names one commit cannot be replayed against another without failing the subject-hash gate. Key rotation and revocation are honored because the trust root is rebuilt from the identity's current keys at each verification.


Proceed on the rebar backend. Authorship stays git-anchored, the S3 option syncs rather than detaches it, and the isolated orphan branch keeps the material off any shared host, so authored-identity signing is feasible on the rebar backend using the rebar authorship substrate directly.

A Jira-only deployment has no rebar authorship substrate and does not participate. Authored-identity signing is an optional rebar-backend capability, absent on Jira, mirroring the dependency-capability pattern. The merge gate does not require an authorship signature on a backend without the capability.
