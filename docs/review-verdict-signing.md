# Signed review verdicts

The review gate signs every review verdict with an asymmetric SSHSIG signature carried in a DSSE envelope, and verifies each signature against a public trust root before it accepts the verdict. A verifier holds only public keys, so it cannot forge a verdict. Each tenant signs with its own key, and a verdict verifies only under an active key of the run's tenant, so a verdict signed with another tenant's key does not verify. Signing is on by default. Set `REVIEW_SIGNING_ENABLED=false` to fall back to the symmetric HMAC attestor.

## Selecting the signed path

Three settings control the signed path.

- `REVIEW_SIGNING_ENABLED` turns the signed path on. It defaults to `true`. Set it to `false` to use the HMAC attestor.
- `REVIEW_SIGNING_KEY_PATH` is the filesystem path to the OpenSSH private key that signs verdicts.
- `REVIEW_SIGNING_KEY` holds the private key itself. When it is set it takes precedence over the path, and the worker writes it once to a private file that only the worker user can read. A deployed environment injects it from the secret store.
- `REVIEW_TRUST_ROOT_PATH` is the filesystem path to the trust-root bundle described below.

Every review stage signs. Plan review, code review, security review, and acceptance verification each carry an SSHSIG-over-DSSE signature when signing is configured.

With signing enabled, a signed stage fails closed when neither `REVIEW_SIGNING_KEY` nor `REVIEW_SIGNING_KEY_PATH` is set, when `REVIEW_TRUST_ROOT_PATH` is unset, or when the run has no tenant. Selection raises `ReviewSigningNotConfigured` rather than downgrading to the symmetric secret, so provision both the signing key and the trust root before running reviews.

The review runtime needs OpenSSH `ssh-keygen` version 8.9 or newer, which the container image installs. The attestor raises a clear error at construction when `ssh-keygen` is missing.

## Provisioning

The review worker runs a signing preflight at startup. When `REVIEW_SIGNING_ENABLED` is true, the worker refuses to start unless the signing key path and the trust-root path are set, the key file exists, the trust-root bundle loads, `ssh-keygen` is available, `TENANT` is set, and the public half of the signing key is an active key of that tenant in the trust root. A review worker without `TENANT`, or one that holds another tenant's key, therefore refuses to start. A misconfiguration surfaces at startup rather than on the first review.

### Local setup

Signing is on by default, so each local tenant needs a signing key of its own and the trust root needs that key before the tenant's review worker starts. Run these once from the repository root for each tenant, `adp` in this example.

1. Generate an Ed25519 signing key for the tenant. `ssh-keygen -t ed25519 -f /tmp/adp-review-key -N "" -C "review-signer@adp" -q`
2. Copy the example bundle once. `cp config/local/review-trust-root.yaml.example config/local/review-trust-root.yaml`
3. Put the generated public key into the bundle. Read `/tmp/adp-review-key.pub`, set the `public_key` of a key entry to that line, set its `tenant` to `adp`, and set a validity window that includes the current date or omit `not_before` and `not_after`. Each tenant has an entry of its own.
4. Set `REVIEW_SIGNING_KEY="<the contents of /tmp/adp-review-key>"` in the tenant's settings file, `config/local/tenants/adp.env`, and delete `/tmp/adp-review-key` and `/tmp/adp-review-key.pub`. Keep `REVIEW_SIGNING_ENABLED=true` and `REVIEW_TRUST_ROOT_PATH=config/local/review-trust-root.yaml` in `config/local/platform.env`.

`inv k8s-reload` gives the key only to the tenant's review worker in `tenant-adp-review`, which runs with `TENANT=adp`. It refuses a tenant without a key while signing is enabled, two tenants that share one key, and a platform `config/local/review-signing-key` file. `config/local/review-trust-root.yaml` and the tenant settings files are gitignored. To run reviews without signing, set `REVIEW_SIGNING_ENABLED=false`.

### Deployed environments

In AWS each tenant's review worker runs on EKS in the `tenant-<tenant>-review` namespace with `TENANT=<tenant>`, under the tenant's own review worker role. The [tenant](../infra/terraform/tenant) module creates the tenant's signing key secret at `<name_prefix>/tenants/<tenant>/review-signing-key`, empty and encrypted with the tenant's key, and only the tenant's review worker role may read it. The EKS deploy mounts it into that review worker alone as `REVIEW_SIGNING_KEY`. The platform secret store holds no review signing key. Provision each tenant once, `adp` in this example.

1. Generate an Ed25519 signing key for the tenant in a private directory. `ssh-keygen -t ed25519 -f ./adp-review-key -N "" -C "review-signer@adp" -q`
2. Write the private key to the tenant's secret. `aws secretsmanager put-secret-value --secret-id agentic-platform/tenants/adp/review-signing-key --secret-string file://adp-review-key`
3. Add the public key from `adp-review-key.pub` to `review-trust-root.yaml` in the platform config directory, `container_config_dir` of the `private-bedrock` root, as a key entry with `tenant: adp`, and apply the root so the deployment carries the bundle at `REVIEW_TRUST_ROOT_PATH`.
4. Delete `adp-review-key` and `adp-review-key.pub`, and run `inv eks-deploy`, or `inv eks-deploy --command "restart tenant-review-workers"` after a later key change.
5. Run `inv tenant-verify-aws --context <eks context> --only review`, which reports each tenant's review worker readiness and fails a review worker whose key is not an active key of its tenant in the trust root. It names the key by its SHA-256 fingerprint and its trust root key ID, never the key.

`inv eks-deploy` names each tenant whose review signing key has no value, because that tenant's review worker refuses to start while signing is enabled. The trust-root bundle holds only public keys, so it ships as a non-secret configuration artifact, with one or more keys for each tenant. Rebuild or repull the platform image so it carries `openssh-client`.

The worker records a trust-root change to the platform audit store at startup when the loaded bundle differs from the last one recorded. The audit event names the actor, the change summary, and the affected key identifiers.

## Trust-root bundle

The trust root is a versioned YAML bundle of public signer keys. Copy `config/local/review-trust-root.yaml.example` to `config/local/review-trust-root.yaml` and set the keys. Only public keys belong in the bundle.

Fields:

- `version` is an integer bundle version.
- `keys` is a non-empty list. Each entry carries an `id` (a stable key identifier), a `tenant` (the tenant whose review worker signs with the key), a `public_key` (an OpenSSH public key line), an optional `not_before` and `not_after` (ISO-8601 timestamps that bound when the key is trusted), and an optional `revoked` flag.

The loader refuses a key without `tenant` and names the key ID in the error. The SSHSIG principal of a tenant's keys is `review-signer@<tenant>`. A review worker signs a run's verdict under the principal of the run's tenant, and verification renders only the active keys of that tenant into the `allowed_signers` trust root, so a key of one tenant never verifies a verdict for another tenant's run. The run's tenant is the tenant the execution record of the run names, which the review worker binds to each review activity.

A key is active when it is not revoked and the current time falls within its validity window. Both bounds are optional, so a key with neither is always active until revoked.

## Rotation and revocation

Rotation uses overlapping validity windows within one tenant. Give the new key the same `tenant` and a `not_before` that precedes the old key's `not_after`, so both keys verify during the overlap and the rollout does not require a flag day. After the old key's `not_after` passes, only the new key verifies.

Revocation sets `revoked: true` on the compromised key. A revoked key is never rendered into the trust root, so its signatures stop verifying immediately regardless of its validity window.

## Audit

Every trust-root change writes an audit record naming the actor, the change, the affected key identifiers, and the tenants the bundle holds keys for, through the platform audit path. The change summary reports how many keys were added, removed, or revoked.

## Fail-closed behavior

The gate fails closed and blocks the verdict when a signature is absent, when the envelope is malformed, when the payload does not match the signed bytes, when the signer key is outside the trust root or belongs to a tenant other than the run's tenant, or when the signer key is revoked or out of its validity window. Selection also fails closed before a signed review runs when signing is enabled but the signing key or trust root is unconfigured.
