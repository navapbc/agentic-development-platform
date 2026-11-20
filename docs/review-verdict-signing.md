# Signed review verdicts

The review gate signs every review verdict with an asymmetric SSHSIG signature carried in a DSSE envelope, and verifies each signature against a public trust root before it accepts the verdict. A verifier holds only public keys, so it cannot forge a verdict. Signing is on by default. Set `REVIEW_SIGNING_ENABLED=false` to fall back to the symmetric HMAC attestor.

## Selecting the signed path

Three settings control the signed path.

- `REVIEW_SIGNING_ENABLED` turns the signed path on. It defaults to `true`. Set it to `false` to use the HMAC attestor.
- `REVIEW_SIGNING_KEY_PATH` is the filesystem path to the OpenSSH private key that signs verdicts.
- `REVIEW_SIGNING_KEY` holds the private key itself. When it is set it takes precedence over the path, and the worker writes it once to a private file that only the worker user can read. A deployed environment injects it from the secret store.
- `REVIEW_TRUST_ROOT_PATH` is the filesystem path to the trust-root bundle described below.

Every review stage signs. Plan review, code review, security review, and acceptance verification each carry an SSHSIG-over-DSSE signature when signing is configured.

With signing enabled, a signed stage fails closed when neither `REVIEW_SIGNING_KEY` nor `REVIEW_SIGNING_KEY_PATH` is set, or when `REVIEW_TRUST_ROOT_PATH` is unset. Selection raises `ReviewSigningNotConfigured` rather than downgrading to the symmetric secret, so provision both the signing key and the trust root before running reviews.

The review runtime needs OpenSSH `ssh-keygen` version 8.9 or newer, which the container image installs. The attestor raises a clear error at construction when `ssh-keygen` is missing.

## Provisioning

The review worker runs a signing preflight at startup. When `REVIEW_SIGNING_ENABLED` is true, the worker refuses to start unless the signing key path and the trust-root path are set, the key file exists, the trust-root bundle loads, and `ssh-keygen` is available. A misconfiguration surfaces at startup rather than on the first review.

### Local setup

Signing is on by default, so a local stack needs a signing key and a trust root before the review worker starts. Run these once from the repository root.

1. Generate an Ed25519 signing key. `ssh-keygen -t ed25519 -f config/local/review-signing-key -N "" -C "review-signer@nava" -q`
2. Copy the example bundle. `cp config/local/review-trust-root.yaml.example config/local/review-trust-root.yaml`
3. Put the generated public key into the bundle. Read `config/local/review-signing-key.pub` and set the `public_key` of the single key entry to that line, set `principal` to match the key comment, and set a validity window that includes the current date or omit `not_before` and `not_after`.
4. Set the three variables in `.env`. `REVIEW_SIGNING_ENABLED=true`, `REVIEW_SIGNING_KEY_PATH=config/local/review-signing-key`, and `REVIEW_TRUST_ROOT_PATH=config/local/review-trust-root.yaml`.

The key files and `config/local/review-trust-root.yaml` are gitignored. The compose stack mounts `config/local` into the review worker, so the worker reads them at the paths above. To run reviews without signing, set `REVIEW_SIGNING_ENABLED=false`.

### Deployed environments

Store the private signing key in the `review_signing_key` secret of the [secret-store](../infra/terraform/secret-store) module and inject it into the review worker as `REVIEW_SIGNING_KEY`. The trust-root bundle holds only public keys, so ship it as a non-secret configuration artifact at `REVIEW_TRUST_ROOT_PATH`. Rebuild or repull the review-worker image so it carries `openssh-client`.

The worker records a trust-root change to the platform audit store at startup when the loaded bundle differs from the last one recorded. The audit event names the actor, the change summary, and the affected key identifiers.

## Trust-root bundle

The trust root is a versioned YAML bundle of public signer keys. Copy `config/local/review-trust-root.yaml.example` to `config/local/review-trust-root.yaml` and set the keys. Only public keys belong in the bundle.

Fields:

- `version` is an integer bundle version.
- `principal` is the SSHSIG principal that every trusted key binds to. It must match the principal the signing key signs under.
- `keys` is a non-empty list. Each entry carries an `id` (a stable key identifier), a `public_key` (an OpenSSH public key line), an optional `not_before` and `not_after` (ISO-8601 timestamps that bound when the key is trusted), and an optional `revoked` flag.

A key is active when it is not revoked and the current time falls within its validity window. Both bounds are optional, so a key with neither is always active until revoked.

## Rotation and revocation

Rotation uses overlapping validity windows. Give the new key a `not_before` that precedes the old key's `not_after`, so both keys verify during the overlap and the rollout does not require a flag day. After the old key's `not_after` passes, only the new key verifies.

Revocation sets `revoked: true` on the compromised key. A revoked key is never rendered into the trust root, so its signatures stop verifying immediately regardless of its validity window.

## Audit

Every trust-root change writes an audit record naming the actor, the change, and the affected key identifiers, through the platform audit path. The change summary reports how many keys were added, removed, or revoked.

## Fail-closed behavior

The gate fails closed and blocks the verdict when a signature is absent, when the envelope is malformed, when the payload does not match the signed bytes, when the signer key is outside the trust root, or when the signer key is revoked or out of its validity window. Selection also fails closed before a signed review runs when signing is enabled but the signing key or trust root is unconfigured.
