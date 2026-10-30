# Authored-identity signing

Each authoring agent signs the plan it produces and the code change it commits under its own rebar identity. A merge gate verifies both signatures against the identity trust root and fails closed on any absent, invalid, or out-of-trust-root signature. Signing is on by default, and it is active only on the rebar ticket backend with an identity configured. A Jira deployment has no rebar authorship substrate and does not participate.

The coupling evaluation that gates this capability is recorded in [authored-identity-coupling-evaluation.md](authored-identity-coupling-evaluation.md).

## Configuration

- `AUTHORSHIP_SIGNING_ENABLED` turns the capability on. It defaults to `true`. Set it to `false` to skip authorship signing.
- `AUTHORSHIP_IDENTITY_ID` is the rebar identity ticket id for the authoring agent. Signing is inactive when it is unset, so a deployment that does not set it does not participate.
- `AUTHORSHIP_SIGNING_KEY_PATH` is the filesystem path to the agent's OpenSSH private signing key.
- `AUTHORSHIP_SIGNING_KEY` holds the private key itself. When it is set it takes precedence over the path, and the worker writes it once to a private file with its derived `.pub` counterpart alongside. A deployed environment injects it from the secret store.

The capability is active only when `AUTHORSHIP_SIGNING_ENABLED` is true, the ticket backend is rebar, and `AUTHORSHIP_IDENTITY_ID` is set. On a capable backend a missing key raises rather than silently skipping, so a run never merges an unsigned change when signing is expected.

## Identities and keys

An authoring agent is a rebar identity ticket carrying an event-sourced keyring. The genesis key is admitted trust-on-first-use. `create_authoring_identity` mints the identity and seeds the genesis key, returning the identity id to record in `AUTHORSHIP_IDENTITY_ID`. Provision the matching private key to the authoring worker as a file path, the same way other worker secrets reach it.

## Local setup

Run these once from the repository root, with the rebar ticket backend selected.

1. Generate the agent signing key. `ssh-keygen -t ed25519 -f config/local/authorship-signing-key -N "" -C "code-agent@nava" -q`
2. Mint the identity and seed its genesis key from the generated public key, then record the returned id in `AUTHORSHIP_IDENTITY_ID`. Use `services.orchestration.authorship.create_authoring_identity` against the rebar store.
3. Set `AUTHORSHIP_SIGNING_KEY_PATH=config/local/authorship-signing-key` in `.env`.

The signing key files are gitignored. The compose stack mounts `config/local` into the activity worker, so the worker reads the key at the path above.

## Startup preflight

The activity worker runs an authorship signing preflight at startup. When the capability is active it refuses to start unless `ssh-keygen` is available, the signing key file exists, the configured identity resolves in the rebar store as an identity ticket, and the identity resolves a trust root, seeding the genesis key trust-on-first-use when the identity is keyless. A misconfiguration surfaces at startup with an actionable message rather than on the first signed run.

## Deployed environments

Store the private signing key in the `authorship_signing_key` secret of the [secret-store](../infra/terraform/secret-store) module and inject it into the activity worker as `AUTHORSHIP_SIGNING_KEY`. Provision the identity and its genesis key in the rebar store out of band, then set `AUTHORSHIP_IDENTITY_ID` to the identity id. Rebuild or repull the activity-worker image so it carries `openssh-client`. The startup preflight validates this provisioning before the worker accepts work.

## Rotation and revocation

Rotation admits a new key through a key-operation signed by a currently-valid key, so a new key is trusted before the old key retires. Revocation removes a compromised key through a signed key-operation, after which the revoked key no longer resolves into the trust root and its signatures stop verifying. Every keyring change writes an audit record naming the actor, the operation, the identity, and the affected key.

## Fail-closed behavior

The merge gate blocks the merge when a required plan or code authorship signature is absent, when a signature is tampered or malformed, when the signer key is outside the identity trust root, or when the signer key is revoked. On a backend without the authorship capability the merge gate requires no signature and the merge proceeds unchanged.
