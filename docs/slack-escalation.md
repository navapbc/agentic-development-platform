# Slack escalation

When a run parks for a human, the activity worker of the run's tenant posts the alert to the tenant's Slack channel. A gate park carries Approve and Reject buttons, a repair park after exhausted repairs carries Retry repair and Abandon, and a park on a spent run or tenant model budget carries Abandon only. A click resolves the park as `inv gate`, `inv repair --retry`, or `inv repair --abandon` would, and records the Slack user as the actor. Every message keeps the `Next step:` line, so the command still works for an operator who does not use Slack.

The same message reaches commercial Slack, Enterprise Grid, and GovSlack. The Jira comment and the alert email are delivered before Slack, and a Slack failure leaves `slack` out of the alert's channels without failing the park.

A tenant posts through its own Slack app, or through the deployment's shared app when `slack.app` is `shared` in its `escalation.yaml`. A tenant uses one or the other, never both. [The shared app](#the-shared-app) describes the second option.

## One app per tenant

A tenant on its own app creates an internal Slack app from [`config/platform/slack-app-manifest.yaml.example`](../config/platform/slack-app-manifest.yaml.example), at `https://api.slack.com/apps` for commercial Slack or `https://api.slack-gov.com/apps` for GovSlack. No two tenants share an app, a token, a signing secret, or an action key, and the render, `inv tenant-config-validate`, the publish, and the inbound webhook refuse a configuration in which they do. A tenant's own app may not declare the shared app's ID.

| Credential | Setting | Read by |
|---|---|---|
| Bot token, `xoxb-` | `SLACK_BOT_TOKEN` | The tenant's activity worker, which posts and updates messages |
| App-level token, `xapp-` with `connections:write` | `SLACK_APP_TOKEN` | The tenant's activity worker, for Socket Mode |
| Signing secret | `SLACK_SIGNING_SECRET`, delivered to the webhook as `SLACK_SIGNING_SECRET_<TENANT>` | The inbound webhook, for request URL clicks |
| Action key | `ESCALATION_ACTION_KEY`, delivered to the webhook as `ESCALATION_ACTION_KEY_<TENANT>` | The tenant's activity worker, which signs the buttons, and the inbound webhook |

Locally each tenant sets these in its own settings file, `config/local/tenants/<tenant>.env`, and `config/local/platform.env` refuses them. Generate the action key with `openssl rand -hex 32`. In AWS the bot and app tokens are the tenant's `slack_bot_token` and `slack_app_token` secrets, the signing secret is its `slack_signing_secret` webhook secret, and the tenant module generates `escalation_action_key`, which only the tenant's worker role and the webhook reader read.

With a bot token and a `slack` block in `escalation.yaml`, the bot posts and `SLACK_WEBHOOK_URL` stays unused. Without a bot token, `SLACK_WEBHOOK_URL` posts the alert text with no buttons, as before.

### Creating the app

Creating and installing an app needs that right in the workspace. A member without it asks a workspace admin to create the app and add them under **Basic Information**, **Collaborators**, or tests in a workspace they own. Every page named below is in the left sidebar of the app's settings, which open from the app's name at `https://api.slack.com/apps`, or `https://api.slack-gov.com/apps` for GovSlack.

The steps copy two kinds of value. The tokens and the signing secret are secrets and go in the tenant's settings file, `config/local/tenants/<tenant>.env`. The IDs named `slack.<key>` are not secrets and go under `slack:` in the tenant's `escalation.yaml`, `config/local/tenants/<tenant>/escalation.yaml`, copied from the `escalation.yaml.example` of the tenant template.

1. Create the app.
   1. Sign in to the target workspace in the browser. The workspace list in the next step offers only workspaces the browser is signed in to.
   2. Open `https://api.slack.com/apps` and choose **Create New App**, then **From a manifest**.
   3. In **Pick a workspace to develop your app in**, choose the workspace that holds the escalation channel, and choose **Next**. The app belongs to that workspace.
   4. Select the **YAML** tab, replace its content with [`config/platform/slack-app-manifest.yaml.example`](../config/platform/slack-app-manifest.yaml.example), and choose **Next**. For GovSlack, first uncomment the manifest's `compliance` section, which GovSlack shows on the app and commercial Slack refuses. For a tenant on the `webhook` transport, set `socket_mode_enabled` to `false` and `request_url` to `https://<webhook host>/webhook/slack/<tenant>`. Give each tenant's app its own `display_information.name` and `features.bot_user.display_name`.
   5. Review the summary and choose **Create**. The app opens on **Basic Information**.
2. Generate the app-level token, for the `socket` transport.
   1. On **Basic Information**, scroll to **App-Level Tokens** and choose **Generate Token and Scopes**.
   2. In **Token Name**, enter `adp-socket-mode`. The name is a label shown only in this list.
   3. Choose **Add Scope**, select `connections:write`, and choose **Generate**.
   4. Copy the token, which starts with `xapp-`, into `SLACK_APP_TOKEN`, and choose **Done**. Choosing the token's name in the list shows it again.
3. Confirm Socket Mode, for the `socket` transport. Open **Socket Mode** under **Settings** and check that **Enable Socket Mode** is on. The manifest turns it on. Turning it on by hand, with no app-level token yet, opens the same token dialog as step 2.
4. Confirm interactivity. Open **Interactivity & Shortcuts** under **Features** and check that **Interactivity** is on. With Socket Mode on, the page shows that payloads arrive over the socket, and no request URL is needed.
5. Copy the app's IDs. On **Basic Information**, under **App Credentials**, copy **App ID**, which starts with `A`, into `slack.app_id`. For the `webhook` transport, choose **Show** beside **Signing Secret** and copy it into `SLACK_SIGNING_SECRET`.
6. Install the app.
   1. Open **OAuth & Permissions** under **Features**. Under **Scopes**, **Bot Token Scopes** lists `chat:write`.
   2. Under **OAuth Tokens**, choose **Install to Workspace**, then **Allow**. A workspace that requires approval shows **Request to Install**, and the token appears once an admin approves.
   3. Copy the **Bot User OAuth Token**, which starts with `xoxb-`, into `SLACK_BOT_TOKEN`.
7. Invite the bot. In the Slack client, open the escalation channel and send `/invite @<bot display name>`. Choose the channel's name at the top, and copy the **Channel ID** at the bottom of the **About** tab, which starts with `C`, into `slack.channel`.
8. Find the organization. Run `curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" https://slack.com/api/auth.test`, against `https://slack-gov.com/api/auth.test` for GovSlack. Set `slack.organization_id` to its `enterprise_id` when the response has one, because a click from an Enterprise Grid workspace carries the enterprise ID, and to its `team_id` otherwise.
9. Name the approvers. For each approver, open their profile in the Slack client, choose **⋮**, then **Copy member ID**, and add it to `slack.allowlist` as a `user_id`.

## The shared app

A deployment may define one shared Slack app, installed into one workspace or Enterprise Grid org, which every tenant in that workspace can select instead of creating its own. Each tenant on the shared app names its own channel, allowlist, content level, button lifetime, and profile, and keeps its own action key. The platform holds the shared app's credentials, and no tenant pod receives them.

| Credential or setting | Where it lives | Read by |
|---|---|---|
| App ID, organization ID, and API base URL | The platform file `slack-shared-app.yaml`, `config/local/slack-shared-app.yaml` locally, from [`config/local/slack-shared-app.yaml.example`](../config/local/slack-shared-app.yaml.example), or the `slack_shared_app` variable of the private-bedrock example | The worker, the inbound webhook, the posting service, and `inv tenant-config-validate` |
| Bot token, `xoxb-` | `SLACK_SHARED_BOT_TOKEN`, or the secret store's `slack_shared_bot_token` | The Slack posting service only |
| Signing secret, for the request URL | `SLACK_SHARED_SIGNING_SECRET`, or the secret store's `slack_shared_signing_secret` | The inbound webhook only |
| App-level token, `xapp-` with `connections:write`, for Socket Mode | `SLACK_SHARED_APP_TOKEN`, or the secret store's `slack_shared_app_token` | The inbound webhook only |
| Action key | Each tenant's `ESCALATION_ACTION_KEY`, as for a tenant's own app | The tenant's activity worker and the inbound webhook |
| Server certificate and key | Locally, issued by the render from a CA kept in `$XDG_STATE_HOME/agentic-dev-platform/slack-poster-tls`, else `~/.local/state/agentic-dev-platform/slack-poster-tls`. In AWS, the secret store's `slack_poster_tls`, issued from the platform CA by `inv slack-poster-certs` | The Slack posting service only, and each tenant worker verifies it against the `slack-poster-ca` ConfigMap |

The render refuses `SLACK_SHARED_BOT_TOKEN`, `SLACK_SHARED_SIGNING_SECRET`, and `SLACK_SHARED_APP_TOKEN` in a tenant's env file, and keeps each out of every pod's environment but the one that reads it.

### Creating the shared app

A workspace admin creates the app once per deployment, following [Creating the app](#creating-the-app) with these differences.

1. Run `inv tenant-config-claims backfill --bucket <config bucket>` before any tenant publishes `app: shared`, and resolve every clash it reports. Without it, a tenant on its own app whose bundle predates claims loses its channel to a shared tenant that names the same channel ID, as [the tenant config doc](tenant-config.md#backfill-and-enforcement) describes.
2. Use [`config/platform/slack-shared-app-manifest.yaml.example`](../config/platform/slack-shared-app-manifest.yaml.example), and choose how the app delivers clicks. A Slack app delivers them over one transport, so each deployment's app takes one of these.
   - The request URL, the default in AWS. Set `request_url` to `https://<webhook host>/webhook/slack/_shared`, leave Socket Mode off, and create no app-level token.
   - Socket Mode, for a deployment without a public URL, such as the local stack. Set `socket_mode_enabled` to `true` and remove `request_url`. On **Basic Information**, under **App-Level Tokens**, generate a token with `connections:write`. The inbound webhook opens the connection out to Slack, so no tunnel or relay is needed. In AWS, add the WebSocket hosts `apps.connections.open` returns to the DNS Firewall, as [Network](#network) describes.
3. In AWS, uncomment `allowed_ip_address_ranges` and list the NAT gateway egress addresses. Slack then refuses the bot token from outside the VPC. Every pod in the VPC shares those addresses, so the restriction does not separate the posting service from other pods.
4. Copy the **App ID** and the organization from `auth.test` into `slack-shared-app.yaml`, or into the `slack_shared_app` variable. Copy the **Bot User OAuth Token** into `SLACK_SHARED_BOT_TOKEN`, and for the request URL the **Signing Secret** into `SLACK_SHARED_SIGNING_SECRET`, or for Socket Mode the app-level token into `SLACK_SHARED_APP_TOKEN`, or into the matching Secrets Manager secrets.
5. Run `inv k8s-reload`, or apply Terraform and run `inv eks-deploy`. The `slack-poster` deployment exists only while the shared app file and its bot token are present, the `/webhook/slack/_shared` route only while the signing secret is set, and the Socket Mode connection only while the app-level token is set.
6. Invite the bot to each shared tenant's channel.

### A tenant on the shared app

```yaml
config_version: 1
channels: [jira, email, slack]
slack:
  app: shared
  transport: webhook
  channel: C0123456789
  profile: private
  content: full
  allowlist:
    gate:
      - user_id: U0123456789
```

Validation refuses a shared tenant's file that sets `app_id`, `organization_id`, or `api_base_url`, names the `socket` transport, or names a channel ID that is not a `C` ID. A private channel shared through Slack Connect receives a new `C` ID, which is why `G`, `D`, `U`, and `W` IDs are refused. A `government` tenant on the shared app is held to `approved_hosts` and to `minimal` content, with the shared app's API host as its host. Validation also refuses the file when no `slack-shared-app.yaml` sits in the platform directory, so tenant CI passes `--platform-dir`. The platform refuses `app: shared` when neither `TENANT` nor `TENANTS` names a tenant, in `load_escalation_config`, `inv config-check`, the renders, and the posting service's start, because the shared app posts only for a tenant's activity worker. The webhook refuses the Slack settings of every tenant that holds another tenant's `ESCALATION_ACTION_KEY_<TENANT>`, and leaves a shared tenant's leftover `SLACK_SIGNING_SECRET_<TENANT>` out of the signing-secret comparison, since a shared tenant's clicks verify under the shared signing secret.

The channel is a claim. The render, `inv tenant-config-validate`, and the publish refuse a bundle that names a channel another tenant holds, as [the tenant config doc](tenant-config.md#claims) describes.

### Posting and clicks

A shared tenant's activity worker posts the alert to the `slack-poster` service at `SLACK_POSTING_URL` over HTTPS, verifies the service's certificate against the CA file `SLACK_POSTING_CA_FILE` names and no other trust store, and sends a projected service account token for the `slack-posting-service` audience that the kubelet replaces every 600 seconds. The service verifies the token through TokenReview and accepts it only from the `activity-worker` service account in a `tenant-<tenant>` namespace, from a pod whose name starts with `activity-worker-`. It takes the tenant from that identity and posts to the channel the tenant's latest `escalation.yaml` names, through `chat.postMessage` with unfurling off. The request may carry only `text` and `blocks`, so a worker cannot name a channel.

| Response | Meaning |
|---|---|
| 200 | Slack accepted the post, and the reply carries its `ts` |
| 400 | The body or the deadline header is malformed |
| 401 | The token is missing, or TokenReview did not accept it for a tenant activity worker |
| 403 | The tenant is not on the shared app, or does not hold its channel |
| 429 | The tenant exceeds its post rate or in-flight cap, or Slack rate-limited the app |
| 502 | Slack refused the post, and the reply carries the Slack error code |
| 503 | The service is not ready, or the Kubernetes API is unreachable |
| 504 | Slack did not answer within the alert's deadline |

Any response other than 200 leaves `slack` out of the alert's channels, and the worker's warning names the error code. The service holds no database credential, and it logs each post as `slack_post_delivered` and each refusal as `slack_post_refused` with the tenant, the channel, and the reason, never the body or the token. Its readiness probe fails until the service holds a valid `slack-shared-app.yaml` and `auth.test` accepts the bot token and names that file's organization. The service repeats `auth.test` every 30 seconds until it does, and again after a refresh changes the shared app, so a Slack outage or a malformed file at startup heals without a restart. The Slack call gets the alert's deadline less the time the token review took, and a post with under a second left is refused with 504 before it reaches Slack. The service answers 504 once the deadline passes, and a post Slack received before then can still appear in the channel. A worker with under three seconds left sends no request, because the service would have no time left for Slack.

The posting service refuses the tenants that share an action key, as the webhook does, without holding any key. It compares each tenant's SHA-256 fingerprint, `ESCALATION_ACTION_KEY_SHA256_<TENANT>`, which the local render computes from each tenant's key and which in AWS the tenant module writes beside the key. A tenant without a fingerprint is served, and the service logs that a key it shares with another tenant goes undetected.

### Action key fingerprints in AWS

The tenant module writes each tenant's action key and its fingerprint at `escalation_action_key_version`, 1 by default, from one generated value. A tenant created by a release without the fingerprint has a fingerprint that does not match its key, and `inv tenant-verify-aws` reports the mismatch in its `slack` group. To match them:

1. Set `escalation_action_key_version = 2` on the tenant in the `tenants` variable of `private-bedrock`, and apply.
2. Run `inv eks-deploy --command "restart inbound-webhook slack-poster tenant-workers"`, because each reads its keys when it starts.
3. Run `inv tenant-verify-aws --context <context>`, and confirm that the fingerprint check of the `slack` group passes.

The new key replaces the old one, so every button already posted for the tenant stops verifying, and its `Next step:` command still works.

### The posting service certificate

The posting service serves a certificate for `slack-poster.<namespace>.svc.cluster.local` and `127.0.0.1`, which its health probe calls. Every tenant worker mounts the `slack-poster-ca` ConfigMap and verifies the service against it, so a post to a service with another certificate fails as unreachable and names the verification failure. Locally, the render issues the CA once and a leaf from it, reuses both on every `inv k8s-reload`, and issues a new leaf when it is within 30 days of its expiry or the namespace changes. In AWS, the ConfigMap carries every certificate of `PLATFORM_CA_PEM`, and the leaf comes from the secret store's `slack_poster_tls`. A worker reads a changed CA file on its next post. The service logs `slack_poster_leaf_expiring` at start and once a day while the leaf it loaded is within 30 days of its expiry, and `slack_poster_leaf_renewed` while the mounted file holds a renewed leaf that only a restart serves.

Reissue the AWS leaf before its 397-day expiry from the directory of the platform CA, which `inv temporal-certs generate --internal-name` wrote:

```bash
inv slack-poster-certs issue --dir <CA directory> --force
inv slack-poster-certs put --dir <CA directory>
```

The secret store restarts `slack-poster` after the write. To rotate the platform CA, add the new CA to the file `container_platform_ca_file` names and deploy, so `slack-poster-ca` holds both CAs. Then reissue and put the leaf from the new CA, and remove the old CA from the file and deploy once `slack-poster` has rolled.

A click on a shared app message arrives at `/webhook/slack/_shared`, verifies under the shared signing secret and app ID, and goes to the tenant whose `escalation.yaml` names the click's channel. That tenant's action key, organization, channel, and allowlist checks then apply as for a tenant's own app. A click from a channel no shared tenant names is refused with an ephemeral reply and a `SLACK_ACTION_REFUSED` event whose reason is `unrouted` and that names the route and the channel. With a tenant config bucket, the webhook and the posting service serve a shared tenant on its channel only when its claim marker is the earliest of every marker that contends for the channel, including the markers of tenants that no longer name it. A shared tenant without a marker on its channel, or whose marker ties with another, is refused, and so is a tenant on its own app whose marker on the channel is later than a shared tenant's. Without a bucket, one tenant naming the channel is served and two are both refused. When the claim listing of a channel fails, the channel keeps its last decision, and its tenants are refused until a listing succeeds after a start.

### The shared rate budget

Slack limits each Web API method per app per workspace, so every shared tenant draws on one budget. The posting service holds each tenant to 0.5 posts per second with a burst of 2 and at most 2 posts in flight, and caps each Slack call at 5 seconds. When Slack answers 429, the service stops calling `chat.postMessage` for every tenant for the `Retry-After` seconds, or 60 seconds without the header, and answers each post in that window with 429 and the remaining wait. Continued calls during a 429 risk Slack disabling the app for every tenant. The service runs one replica, because the limits are held in process.

### Slack Connect

A shared channel admits members of another organization. Alert content posted there is visible to them, so a tenant whose channel is shared through Slack Connect sets `content: minimal`. Their clicks are refused, because the dispatcher checks the clicking user's organization and the allowlist.

## escalation.yaml

The file sits only in the tenant's config bundle, `config/local/tenants/<tenant>/escalation.yaml` locally or the bundle in the tenant config bucket, and a copy in `config/local/` is refused. A tenant without the file delivers to every channel whose settings are present.

```yaml
config_version: 1
channels: [jira, email, slack]
slack:
  transport: socket
  channel: C0123456789
  organization_id: E0123456789
  app_id: A0123456789
  profile: private
  content: full
  api_base_url: https://slack.com/api/
  token_ttl_hours: 72
  approved_hosts: []
  allowlist:
    gate:
      - user_id: U0123456789
    repair:
      - user_id: U0123456789
```

| Key | Meaning |
|---|---|
| `app` | `own`, the default, or `shared`. With `shared`, the file sets no `app_id`, `organization_id`, or `api_base_url`, which come from the shared app, `transport` is `webhook`, and `channel` is a `C` channel ID |
| `transport` | `socket` or `webhook`. Slack stops delivering an app's clicks to its request URL while Socket Mode is on, so an app runs one transport |
| `channel` | The channel ID the bot posts to. Invite the bot to it |
| `organization_id` | The Enterprise Grid ID, starting with `E`, or the workspace ID, starting with `T`. A click from any other organization or channel is refused |
| `app_id` | The app's ID. The webhook refuses a click signed for another app |
| `profile` | The tenant's `gates.yaml` profile. Validation, the worker, and the inbound webhook refuse a file in which the two differ |
| `content` | `full` carries the repository and the bounded failure detail. `minimal` carries the issue key, the stage, the reason code, and the commands of the next step, without the next step's prose, which can name the failure or the repository. Both levels link the issue key to its Jira ticket, at the `/browse/` path of the site `jira.yaml` names for the project. A rebar ticket has no web view, so its key is not linked. A `government` profile defaults to `minimal` |
| `api_base_url` | `https://slack.com/api/` by default, or `https://slack-gov.com/api/` for GovSlack |
| `token_ttl_hours` | How long a button stays valid after the park |
| `approved_hosts` | The Slack hosts a `government` tenant may reach |
| `allowlist` | The users who may decide each kind of park, by `user_id` and an optional `organization_id` |

Bundle validation, `inv config-check`, and the activity worker refuse a `government` tenant whose API host is outside `approved_hosts` or whose content is `full`. A refused bundle never becomes active, so the last compliant bundle stays in force. The inbound webhook refuses that tenant's Slack route and keeps serving every other tenant.

## Transports

### Socket Mode

The tenant's activity worker opens a WebSocket to Slack with the app-level token, against the tenant's API host, so a GovSlack tenant connects to `slack-gov.com`. No inbound endpoint is needed. The client acknowledges each click on receipt, hands it to the dispatcher on the worker's event loop, and replaces the message through `chat.update` once the decision is applied. A Slack outage at startup does not stop the worker, so the tenant's runs and its Jira and email alerts continue. The client logs the failure and retries after 5 seconds, 15 seconds, and then every 60 seconds. Until it connects, the buttons in the channel do not respond, and the `Next step` command in the alert still applies the decision.

### Request URL

The inbound webhook accepts each tenant's clicks at `/webhook/slack/<tenant>`, and the shared app's clicks at `/webhook/slack/_shared`. The per-tenant route answers 404 for a tenant on the shared app. The shared route serves whenever `SLACK_SHARED_SIGNING_SECRET` is set, reads the shared app on each request, and answers 503 while no valid `slack-shared-app.yaml` has been read, so Slack retries the click. A malformed shared app file or a failed claim listing leaves the PR, Jira, and per-tenant Slack routes serving. It refuses a missing, non-numeric, or more than five minutes skewed timestamp and an invalid `v0` signature with 401 before it reads the body, checks the app ID, applies the decision within two seconds, and responds. It then updates the message through the click's `response_url`, which it calls only over HTTPS on the `/actions/` path of `hooks.slack.com`, or of `hooks.slack-gov.com` for a GovSlack tenant, without following redirects. The webhook holds no bot token. With `SLACK_SHARED_APP_TOKEN` set, the shared app's clicks arrive over a Socket Mode connection the webhook opens to the shared app's API host instead. The connection authenticates them in place of the signature, and each passes the same app ID, channel, action key, and allowlist checks and is answered through its `response_url` the same way. The connection waits for a valid `slack-shared-app.yaml` and reconnects after an outage. It reads each tenant's `escalation.yaml` and `gates.yaml` from the tenant's bundle at startup and again on a background thread every `TENANT_CONFIG_REFRESH_SECONDS`, so a click never waits on a bundle read. A file that fails validation leaves the last valid settings in force.

## What a click does

Each button carries a token signed with HMAC-SHA256 under the tenant's action key. It binds the tenant, the execution, the workflow, the park, the decision kind, the permitted action, and an expiry. The dispatcher checks the token, the tenant, the organization and channel, and the allowlist, then sends the decision to the workflow as a Temporal Update.

The workflow accepts a decision only for the park it is waiting on. Each park has a key built from the execution, the stage, the gate list or repair attempt, and the park's ordinal in the run. A decision that arrives while the alert is still being sent, or that names an earlier park of the same run, is refused, and the first decision for a park stands. The Update ID derives from the park, so a double click, a retried click, and a click on either transport resolve to the same decision.

| Outcome | What the operator sees |
|---|---|
| Applied | The message is replaced by the decision and its actor, without buttons |
| Already decided, by Slack or by `inv gate` or `inv repair` | An ephemeral reply naming the decision and its actor |
| Earlier park | An ephemeral reply pointing at the most recent message |
| Not on the allowlist, wrong organization or channel, expired, or unverifiable | An ephemeral reply with the reason. The `Next step:` command still works |
| Not confirmed within two seconds | An ephemeral reply to click again, which reports the outcome |

The workflow records the decision in the `HUMAN_INTERVENTION_DECISION` audit event with `actor_type` `slack` and `actor_id` `slack:<organization>/<user>`, and writes a decision evidence record. A decision from `inv gate` or `inv repair` records `actor_type` `operator`. Each refused click writes a `SLACK_ACTION_REFUSED` audit event with the tenant, the reason, the Slack organization and user, and a digest of the park key, and never the token, the `response_url`, or the payload. The platform pins the `slack_sdk` loggers at INFO, because at DEBUG they would log the tokens.

A run that started before this change parks without a key. Its alert carries no buttons, and `inv gate` and `inv repair` resolve it as before.

## Network

The DNS Firewall allows `hooks.slack.com` by default. A deployment that uses the Web API adds `slack.com`, and a GovSlack deployment adds `slack-gov.com` and `hooks.slack-gov.com`, through `dns_firewall_additional_domains`. Socket Mode connects to the WebSocket host `apps.connections.open` returns, which release verification records for each deployment, from the tenant's activity worker for a tenant's own app and from the inbound webhook for the shared app. Slack sends request URL clicks from unpublished addresses, so the webhook relies on the signature rather than an address allowlist. With the shared app, the `slack-poster` pod calls the Web API, and the `slack-poster-ingress` NetworkPolicy admits only tenant activity worker pods to its port. Both environments apply the policy, and a local cluster enforces it only when its network plugin enforces NetworkPolicy.

## Moving a tenant between apps

A move changes only the tenant's bundle, because every tenant worker already mounts the posting service token and may reach the service. A run keeps the app its pinned bundle names, so resolve open parks first.

From a tenant's own app to the shared app:

1. Resolve or abandon the tenant's open parks with `inv gate` or `inv repair`. A button posted by the old app stops working once the tenant moves.
2. Set `app: shared` and `transport: webhook` in the tenant's `escalation.yaml`, remove `app_id`, `organization_id`, and `api_base_url`, and publish with `inv tenant-config-publish`. The publish claims the channel, and refuses the bundle when another tenant holds it.
3. Invite the shared bot to the tenant's channel, and remove the old app's bot from it.
4. Delete the retired app's signing secret, `SLACK_SIGNING_SECRET_<TENANT>` locally or the tenant's `slack_signing_secret` in AWS, and its bot and app tokens, then uninstall the old app.

From the shared app to a tenant's own app:

1. Resolve or abandon the tenant's open parks.
2. Create the tenant's app as in [Creating the app](#creating-the-app), and set its credentials.
3. Set `app: own` or remove `app`, and set `transport`, `app_id`, `organization_id`, and `api_base_url`. Publish. The channel claim changes kind, and the app ID becomes a claim.
4. Invite the tenant's bot to the channel, and remove the shared bot from it.

### Changing a channel on the shared app

A tenant on the shared app changes its channel by publishing a new `slack.channel`. The publish keeps the old channel claimed, as a retained claim in the tenant's index, until the `token_ttl_hours` of the replaced `escalation.yaml` plus twice `TENANT_CONFIG_REFRESH_SECONDS` has passed, so no other tenant can claim the old channel while its buttons can still be clicked. Until then, a click on a button in the old channel goes to the tenant that holds the old channel's earliest marker, and is decided under that tenant's action key and allowlist. The posting service posts only to the new channel. The tenant's first publish after the release time deletes the old marker, `inv tenant-config-claims sweep` deletes it for a tenant that does not publish again, and `inv tenant-config-claims release` removes it earlier. A channel another tenant published first is resolved as [A contested value](tenant-config.md#a-contested-value) describes.

1. Invite the shared bot to the new channel.
2. Set `slack.channel` to the new channel ID, and publish with `inv tenant-config-publish`.
3. After the release time, publish again or release the old channel, then remove the shared bot from it.

Without a tenant config bucket no claim is retained, and a click in the old channel is refused with a reply that names the channel change and points to the `Next step:` command in the message.

Two tenants on the shared app swap channels through a third channel. Move the first tenant to a spare channel, wait for its release time and release its old channel, move the second tenant to the freed channel, and repeat for the first. A tenant that publishes another tenant's retained channel is refused, and the refusal names the holder and states that a tenant keeps a channel for its button lifetime after it moves.

## When a credential leaks

A leaked signing secret or app-level token lets its holder act as any allowlisted user, or receive clicks on a connection of their own.

1. Regenerate the signing secret, or revoke the app-level token and create another, in the app's settings.
2. Write the new value to the tenant's settings file and run `inv k8s-reload`, or update the tenant's secret in Secrets Manager and roll the inbound webhook or the tenant's activity worker.
3. Rotate `ESCALATION_ACTION_KEY` the same way, and in AWS raise the tenant's `escalation_action_key_version` as [Action key fingerprints in AWS](#action-key-fingerprints-in-aws) describes. Every button already posted stops verifying, and its `Next step:` command still works.
4. Review the `SLACK_ACTION_REFUSED` and `HUMAN_INTERVENTION_DECISION` audit events since the leak.

A leaked shared app credential reaches every tenant on the shared app. The bot token posts to every channel the bot belongs to. The app-level token lets its holder open a Socket Mode connection that receives a share of the app's clicks. The signing secret, together with a button value and an allowlisted user ID that any channel member can read, forges a decision on an open park of any shared tenant, so the shared signing secret is approval authority across those tenants.

1. Regenerate the signing secret, revoke the app-level token under **App-Level Tokens** and generate another, or choose **Revoke All OAuth Tokens** under **OAuth & Permissions** and reinstall the app to issue a new bot token.
2. Write the new value to `SLACK_SHARED_SIGNING_SECRET`, `SLACK_SHARED_APP_TOKEN`, or `SLACK_SHARED_BOT_TOKEN` in `config/local/platform.env` and run `inv k8s-reload`, or update `slack_shared_signing_secret`, `slack_shared_app_token`, or `slack_shared_bot_token` in Secrets Manager. The secret store restarts the inbound webhook or `slack-poster` after the update. The webhook accepts one signing secret, so every shared tenant's buttons stop responding from the regeneration until the webhook rolls, and each alert's `Next step:` command still works.
3. Review the `slack_post_delivered` and `slack_post_refused` events in the `slack-poster` log, and the `SLACK_ACTION_REFUSED` and `HUMAN_INTERVENTION_DECISION` audit events, since the leak.
