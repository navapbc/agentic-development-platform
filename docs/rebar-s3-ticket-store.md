# rebar S3 ticket store

The rebar ticket event log syncs to an S3 bucket in the platform AWS account rather than to a `tickets` branch on GitHub. The store is still an ordinary git repository. Only the remote changes. `git-remote-s3` writes the same git objects a git host would, as bundle files under a key prefix.

Account-specific values (bucket name, role ARNs, key ARN) are held in repository variables and in the platform's AWS account rather than in this file, because this repository is public.

## Bucket requirements

The bucket must have versioning enabled, SSE-KMS default encryption with a customer-managed key, and all four public-access blocks on. Default bucket encryption is what makes a plain `PutObject` acceptable, because `git-remote-s3` does not request encryption per object. Without it, writes land unencrypted.

A bucket policy should deny non-TLS requests, deny TLS below 1.2, deny any `PutObject` naming a different key, and deny object access from principals other than the store roles.

Two roles divide access. The writer grants `GetObject`, `PutObject`, and `DeleteObject` under the store prefix plus `kms:Decrypt` and `kms:GenerateDataKey`. The reader grants `GetObject` and `kms:Decrypt`. Both constrain `s3:ListBucket` to the store prefix, so listing the bucket root returns AccessDenied by design.

## Local setup

### 1. Install the optional extra

`pyproject.toml` declares `nava-rebar[agents,jira-datacenter,s3]`, which brings in `git-remote-s3>=0.3.2`. That floor matters because 0.3.2 is the first release carrying the `IfNoneMatch` per-ref push lock that prevents multiple-bundles corruption.

### 2. Configure the AWS profile

Add a profile that assumes the writer role on top of your own credentials.

```ini
[profile adp-tickets-writer]
role_arn = <writer role ARN>
source_profile = adp
region = <region>
```

Your IAM principal must appear in the writer role's trust policy. Credentials resolve through the boto3 chain and never enter rebar configuration.

### 3. Add the git remote

`[tool.rebar.sync] remote` names a git remote, and git remotes live in `.git/config`, which is not tracked. Every clone has to add the remote once.

```sh
git remote add s3tickets s3://adp-tickets-writer@<bucket>/<prefix>
```

The `profile@` prefix tells the helper which profile to assume, so the remote works without exporting `AWS_PROFILE`.

### 4. Put the helper on PATH

rebar gates S3 sync behind `shutil.which("git-remote-s3")`, and git resolves `s3://` remotes the same way. Running `.venv/bin/rebar` without activating the virtualenv leaves `.venv/bin` off PATH, and rebar then fails with `OptionalDependencyError` naming the `s3` extra even though it is installed. Activate the virtualenv, or put `.venv/bin` on PATH, before any rebar command that writes.

## Lock refs

The reconciler's pass lock and phase gate use `refs/reconciler/*`, which sit outside the tickets tree. They follow `sync.remote`, so they live beside the store in S3. The helper handles arbitrary ref namespaces, and `git ls-remote --exit-code` reports presence and absence correctly against an `s3://` remote, which is what the ref-lock backend depends on.

## CI

The `Reconcile Bridge` workflow assumes the writer role through GitHub OIDC, so no long-lived access key is stored. The role's trust policy must accept this repository through the account's GitHub OIDC provider. The workflow adds the `s3tickets` remote on every pass, because no clone carries `.git/config`. See `docs/jira-sync-setup.md` for the variables involved.

## Verifying

```sh
git fetch s3tickets 'refs/heads/tickets:refs/remotes/s3tickets/tickets' && git rev-list --left-right --count refs/heads/tickets...refs/remotes/s3tickets/tickets
```

`0 0` means the local store and S3 agree. `rebar fsck` checks store integrity separately.

## Operational notes

The helper clears a stale push lock on its own after `DEFAULT_LOCK_TTL_SECONDS`, which is 60. Override it with `GIT_REMOTE_S3_LOCK_TTL_SECONDS`. The upstream README documents `GIT_REMOTE_S3_LOCK_TTL`, which the code does not read.

`git-remote-s3 --help` exits with an IndexError on 0.4.2 because the entry point expects the argument vector git passes it. Use it only through git.
