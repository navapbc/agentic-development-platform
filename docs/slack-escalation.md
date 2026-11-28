# Slack escalation

When a run parks for a human, the activity worker of the run's tenant posts the alert to the tenant's Slack channel. A gate park carries Approve and Reject buttons, a repair park after exhausted repairs carries Retry repair and Abandon, and a park on a spent run or tenant model budget carries Abandon only. A click resolves the park as `inv gate`, `inv repair --retry`, or `inv repair --abandon` would, and records the Slack user as the actor. A model path outage carries Acknowledge, Run validation, and Fix done, and a failed, reclaimed, plan-rejected, or abandoned run carries Acknowledge and Restart run, as [Alert buttons](#alert-buttons) describes. Every message keeps the `Next step:` line. While the tenant's Slack channel is enabled, that line names the buttons of the post and no command, and the Jira comment and the alert email name the Slack channel of the post. Without Slack, the line names the command of the deployment posture, as [operator-remedies.md](operator-remedies.md) describes.

An alert is routed by who can act on it. A platform fault reaches the tenant as its effect and the statement that work resumes on its own, and its fix reaches the platform administrators, as [Alert audiences](#alert-audiences) and [Platform administrators](#platform-administrators) describe.

The same message reaches commercial Slack, Enterprise Grid, and GovSlack. The Jira comment and the alert email are delivered before Slack, and a Slack failure leaves `slack` out of the alert's channels without failing the park. Each alert is one message per destination, and every follow-up, such as a validation report, a host agent report, or the recovery, is a reply in the thread of that message. The alert activity delivers through the side-effect ledger, keyed on the workflow run and the activity, so an activity retry after a delivery replays the recorded channels and posts no second copy.

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
   1. Open **OAuth & Permissions** under **Features**. Under **Scopes**, **Bot Token Scopes** lists `chat:write` and `usergroups:read`.
   2. Under **OAuth Tokens**, choose **Install to Workspace**, then **Allow**. A workspace that requires approval shows **Request to Install**, and the token appears once an admin approves.
   3. Copy the **Bot User OAuth Token**, which starts with `xoxb-`, into `SLACK_BOT_TOKEN`.
7. Invite the bot. In the Slack client, open the escalation channel and send `/invite @<bot display name>`. Choose the channel's name at the top, and copy the **Channel ID** at the bottom of the **About** tab, which starts with `C`, into `slack.channel`.
8. Find the organization. Run `curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" https://slack.com/api/auth.test`, against `https://slack-gov.com/api/auth.test` for GovSlack. Set `slack.organization_id` to its `enterprise_id` when the response has one, because a click from an Enterprise Grid workspace carries the enterprise ID, and to its `team_id` otherwise.
9. Name the approvers under `slack.allowlist`, for `gate` and for `repair`, in one of the modes [Approvers](#approvers) describes.
   1. Start in channel mode. The template sets `gate: channel` and `repair: channel`, which admit anyone in the channel and organization.
   2. To name users, replace a kind's `channel` with a list. For each approver, open their profile in the Slack client, choose **⋮**, then **Copy member ID**, and add it as a `user_id` entry.
   3. To name a Slack user group, find its ID as [Finding a group ID](#finding-a-group-id) describes, and add it as a `group_id` entry. For an app installed at the Enterprise Grid org level, set the entry's `team_id` to the workspace that owns the group.

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
6. Invite the bot to each shared tenant's channel, and to the administrator channel that `platform-escalation.yaml` names, with `/invite @<bot display name>` in each channel.

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

A shared tenant's activity worker posts the alert to the `slack-poster` service at `SLACK_POSTING_URL` over HTTPS, verifies the service's certificate against the CA file `SLACK_POSTING_CA_FILE` names and no other trust store, and sends a projected service account token for the `slack-posting-service` audience that the kubelet replaces every 600 seconds. The service verifies the token through TokenReview and accepts it only from the `activity-worker` service account in a `tenant-<tenant>` namespace, from a pod whose name starts with `activity-worker-`. It takes the tenant from that identity and posts to the channel the tenant's latest `escalation.yaml` names, through `chat.postMessage` with unfurling off. The request may carry only `text`, `blocks`, `thread_ts`, `audience`, and `owner_token`, so a worker cannot name a channel. `thread_ts` posts the message as a reply in the thread of that message. `POST /v1/messages/update` edits a message through `chat.update`, and a tenant edits only messages in its own channel.

With `audience` set to `admin`, the service posts to the administrator channel that `platform-escalation.yaml` names instead of the tenant's channel. The service also accepts the token of the `inbound-webhook` and `deployment-validate` service accounts in the platform namespace, from a pod whose name starts with the account's name, as the platform caller, which posts only with `audience` set to `admin`.

The answer to a new administrator post carries `owner_token`, an HMAC-SHA256 over the administrator channel, the post's `ts`, and the caller. An edit of an administrator post, or a reply in its thread, must present that token, so a caller edits only the administrator posts it made, and a tenant never edits a post of the platform caller or of another tenant. The tenant worker stores the token of the outage's administrator post on the model path episode. The key derives from `SLACK_SHARED_BOT_TOKEN`, so the tokens hold across a restart of the service. After the shared bot token rotates, or after `platform-escalation.yaml` names another administrator channel, every open administrator post can no longer be edited or replied to, and its alert resolves without the edit.

| Response | Meaning |
|---|---|
| 200 | Slack accepted the post, and the reply carries its `ts` |
| 400 | The body or the deadline header is malformed |
| 401 | The token is missing, or TokenReview did not accept it for a tenant activity worker or the platform caller |
| 403 | The tenant is not on the shared app or does not hold its channel, the platform caller posted without `audience` `admin`, `platform-escalation.yaml` names no administrator channel, or an administrator post edit or reply carried no valid `owner_token` |
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

Slack limits each Web API method per app per workspace, so every shared tenant draws on one budget. The posting service holds each tenant to 0.5 posts per second with a burst of 2 and at most 2 posts in flight, and caps each Slack call at 5 seconds. When Slack answers 429, the service stops calling `chat.postMessage` for every tenant for the `Retry-After` seconds, or 60 seconds without the header, and answers each post in that window with 429 and the remaining wait. Continued calls during a 429 risk Slack disabling the app for every tenant. Group reads hold each tenant to 10 reads per 5 minutes, so a tenant within the group limit reads every group in each refresh, and a 429 on `usergroups.users.list` pauses group reads for every tenant the same way, apart from posts. The service runs one replica, because the limits are held in process.

### Slack Connect

A shared channel admits members of another organization. Alert content posted there is visible to them, so a tenant whose channel is shared through Slack Connect sets `content: minimal`. Their clicks are refused in every approver mode, including `channel`, because the dispatcher checks the clicking user's organization before the approvers.

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
    gate: channel
    repair:
      - user_id: U0123456789
      - group_id: S0123456789
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
| `token_ttl_hours` | How long a button stays valid after it is posted. A park that waits for a decision posts fresh buttons in its alert's thread 4 hours or a quarter of this lifetime before they expire, whichever is shorter, and again before each later expiry until the park is decided. Each fresh post takes the buttons off the earlier posts of the park, and the decision moves every post of the park to the decided state |
| `approved_hosts` | The Slack hosts a `government` tenant may reach |
| `allowlist` | Who may decide each kind of park, `gate` and `repair`. Each kind is `channel`, or a list of `user_id` and `group_id` entries, each with an optional `organization_id`, and a `group_id` entry with an optional `team_id`. A kind the file omits admits nobody. [Approvers](#approvers) describes the modes |

Bundle validation, `inv config-check`, and the activity worker refuse a `government` tenant whose API host is outside `approved_hosts` or whose content is `full`. A refused bundle never becomes active, so the last compliant bundle stays in force. The inbound webhook refuses that tenant's Slack route and keeps serving every other tenant.

## Approvers

Each decision kind under `slack.allowlist`, `gate` and `repair`, admits its approvers in one of three modes. Every mode still requires the click to come from the tenant's `organization_id` and channel, so a Slack Connect user from another organization is refused in each mode. A kind the file omits admits nobody. The decision audit event records what admitted the approver as `approval_basis`, which is `channel`, `user`, or `group:<id>`.

| Mode | Value of `allowlist.<kind>` | Admits |
|---|---|---|
| Channel | `channel` | Anyone in the tenant's channel and organization |
| Users | A list of `user_id` entries | The named users |
| Groups | A list of `group_id` entries | The members of the named Slack user groups |

User and group entries combine in one list, and a click passes when the list names the user or a group the user belongs to. Each entry takes an optional `organization_id`, which defaults to the tenant's `organization_id`. A `group_id` is a Slack user group ID, which starts with `S`, and a `group_id` entry takes an optional `team_id`. Validation refuses an entry that names both or neither of `user_id` and `group_id`, a `group_id` that does not start with `S`, a `team_id` on a `user_id` entry, and a kind whose value is neither `channel` nor a list. The kinds of one file together name at most 10 distinct groups. The channel mode is allowed under every profile, including `government`.

```yaml
allowlist:
  gate: channel
  repair:
    - user_id: U0123456789
    - group_id: S0123456789
      team_id: T0123456789
```

### Group membership

Each tenant's activity worker reads the members of every group its `escalation.yaml` names, at start and every 5 minutes, with `usergroups.users.list`. It records the members with the read time in the `slack_approver_groups` table under its own tenant, and every dispatcher of the tenant reads that record. A tenant on its own app reads through its own bot token against its `api_base_url`. A tenant on the shared app reads through the Slack posting service at `GET /v1/approver-groups/<group_id>`, which answers only a worker it authenticates and only for a group that the worker's tenant names, and refuses any other group with 403 and `group_not_named`. The refresh deletes the record of a group the tenant no longer names.

A failed read keeps the group's last record, and the worker logs the group, the tenant, and the Slack error. When Slack answers `missing_scope`, the log names the `usergroups:read` scope. When Slack answers `missing_argument`, `team_not_found`, or `team_access_not_granted`, the log names `team_id`.

A group record older than 10 minutes no longer admits anyone. A click that only that group would admit is refused with the reason `group_stale`, and its ephemeral reply asks for the tenant's group refresh to be checked and points to the `Next step:` command. The refusal holds until the next read succeeds. A user whom the list also names by `user_id`, or whom a group with a current record admits, is still admitted. A group that has never been read admits nobody, and a click it alone would admit is refused as `not_allowed`.

The groups of the administrator allowlist in `platform-escalation.yaml` are read by the inbound webhook, at start and every 5 minutes, through the posting service as the platform caller, which answers only for a group that allowlist names. The webhook records them under the scope `_platform` in the same table, and deletes the record of a group the allowlist no longer names. A replica runs a cycle only while it holds a Postgres advisory lock and no replica recorded an administrator group in the last 150 seconds, so the replicas of the webhook read Slack once per cycle between them. A failed read keeps the last record, and a record older than 10 minutes is refused as `group_stale`, as for a tenant's group.

### The `usergroups:read` scope

The bot reads groups under the `usergroups:read` scope, which both manifests request. An app created from an earlier manifest holds only `chat:write`, and a group it names admits nobody until the scope is added.

1. Open **OAuth & Permissions** under **Features**, choose **Add an OAuth Scope** under **Bot Token Scopes**, and select `usergroups:read`.
2. Reinstall the app, from the banner Slack shows or from **Reinstall to Workspace** under **OAuth Tokens**. An admin approves the reinstall in a workspace that requires approval.
3. Compare the **Bot User OAuth Token** under **OAuth Tokens** with the configured token. An app with token rotation off, as both manifests set it, keeps its bot token across a reinstall, so no setting changes. If the token differs, copy it into `SLACK_BOT_TOKEN` of the tenant's settings file, or `SLACK_SHARED_BOT_TOKEN` in `config/local/platform.env` for the shared app, and run `inv k8s-reload`. In AWS, update the tenant's `slack_bot_token` secret or the shared `slack_shared_bot_token` secret, and roll the pod that reads it.

### Enterprise Grid and `team_id`

An app installed at the Enterprise Grid org level holds an org-level token, and `usergroups.users.list` then requires the ID of the workspace that owns the group. Set `team_id` on each `group_id` entry to that workspace's ID, which starts with `T`. A workspace install leaves `team_id` out. The read passes `team_id` only when the entry sets it.

### Finding a group ID

User groups need a paid Slack plan. In the Slack client, open the **Home** tab, choose **Directories**, then **User Groups**, and open the group to read its handle. Then list the workspace's groups with `curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" https://slack.com/api/usergroups.list`, against `https://slack-gov.com/api/usergroups.list` for GovSlack, and copy the `id` of the group whose `handle` matches into a `group_id` entry. The bot token answers once the app holds `usergroups:read`. Each group in the response carries a `team_id`, the workspace that owns it, which an org-level install sets as the entry's `team_id`. An org-level token lists the groups of one workspace, named by `?team_id=<workspace ID>` on the URL.

## Transports

### Socket Mode

The tenant's activity worker opens a WebSocket to Slack with the app-level token, against the tenant's API host, so a GovSlack tenant connects to `slack-gov.com`. No inbound endpoint is needed. The client acknowledges each click on receipt, hands it to the dispatcher on the worker's event loop, and replaces the message through `chat.update` once the decision is applied. A Slack outage at startup does not stop the worker, so the tenant's runs and its Jira and email alerts continue. The client logs the failure and retries after 5 seconds, 15 seconds, and then every 60 seconds. Until it connects, the buttons in the channel do not respond, and the `Next step` command in the alert still applies the decision.

### Request URL

The inbound webhook accepts each tenant's clicks at `/webhook/slack/<tenant>`, and the shared app's clicks at `/webhook/slack/_shared`. The per-tenant route answers 404 for a tenant on the shared app. The shared route serves whenever `SLACK_SHARED_SIGNING_SECRET` is set, reads the shared app on each request, and answers 503 while no valid `slack-shared-app.yaml` has been read, so Slack retries the click. A malformed shared app file or a failed claim listing leaves the PR, Jira, and per-tenant Slack routes serving. It refuses a missing, non-numeric, or more than five minutes skewed timestamp and an invalid `v0` signature with 401 before it reads the body, checks the app ID, applies the decision within two seconds, and responds. It then updates the message through the click's `response_url`, which it calls only over HTTPS on the `/actions/` path of `hooks.slack.com`, or of `hooks.slack-gov.com` for a GovSlack tenant, without following redirects. The webhook holds no bot token. With `SLACK_SHARED_APP_TOKEN` set, the shared app's clicks arrive over a Socket Mode connection the webhook opens to the shared app's API host instead. The connection authenticates them in place of the signature, and each passes the same app ID, channel, action key, and allowlist checks and is answered through its `response_url` the same way. The connection waits for a valid `slack-shared-app.yaml` and reconnects after an outage. It reads each tenant's `escalation.yaml` and `gates.yaml` from the tenant's bundle at startup and again on a background thread every `TENANT_CONFIG_REFRESH_SECONDS`, so a click never waits on a bundle read. A file that fails validation leaves the last valid settings in force.

## What a click does

Each button carries a token signed with HMAC-SHA256 under the tenant's action key. It binds the tenant, the execution, the workflow, the park, the decision kind, the permitted action, and an expiry. The dispatcher checks the token, the tenant, the organization and channel, and the kind's approvers, then sends the decision to the workflow as a Temporal Update.

The workflow accepts a decision only for the park it is waiting on. Each park has a key built from the execution, the stage, the gate list or repair attempt, and the park's ordinal in the run. A decision that arrives while the alert is still being sent, or that names an earlier park of the same run, is refused, and the first decision for a park stands. The Update ID derives from the park, so a double click, a retried click, and a click on either transport resolve to the same decision.

| Outcome | What the operator sees |
|---|---|
| Applied | The message is replaced by the decision and its actor, without buttons |
| Already decided, by Slack or by `inv gate` or `inv repair` | An ephemeral reply naming the decision and its actor |
| Earlier park | An ephemeral reply pointing at the most recent message |
| Not on the allowlist, wrong organization or channel, expired, or unverifiable | An ephemeral reply with the reason. The `Next step:` command still works |
| Expired, from an approver of the park's kind, on a park that still waits | The run posts fresh buttons in the alert's thread, at most once a minute, and the ephemeral reply says so. On a park that no longer waits, the reply is the refusal a decision on it would get |
| Admitted only by a group whose record is older than 10 minutes | An ephemeral reply that the group's members have not been read in the last ten minutes, and a `SLACK_ACTION_REFUSED` event with the reason `group_stale` |
| Not confirmed within two seconds | An ephemeral reply to click again, which reports the outcome |

The workflow records the decision in the `HUMAN_INTERVENTION_DECISION` audit event with `actor_type` `slack` and `actor_id` `slack:<organization>/<user>`, records the basis of the approval, `channel`, `user`, or `group:<id>`, as `approval_basis` in its correlation IDs, and writes a decision evidence record. A decision from `inv gate` or `inv repair` records `actor_type` `operator`. Each refused click writes a `SLACK_ACTION_REFUSED` audit event with the tenant, the reason, the Slack organization and user, and a digest of the park key, and never the token, the `response_url`, or the payload. The platform pins the `slack_sdk` loggers at INFO, because at DEBUG they would log the tokens.

A run that started before this change parks without a key. Its alert carries no buttons, and `inv gate` and `inv repair` resolve it as before.

## Alert buttons

Each tenant alert names its buttons in its text, and each button carries a token signed under the tenant's action key that binds the tenant, the alert, the permitted action, and an expiry.

| Alert | Buttons | Allowlist kind |
|---|---|---|
| Gate park | Approve, Reject | `gate` |
| Repair park after exhausted repairs | Retry repair, Abandon | `repair` |
| Re-sync conflict after Retry repair, which names each conflicting file | Retry repair, Abandon | `repair` |
| Park on a reached run limit | Request increase, Acknowledge, Park all jobs, Abandon | `repair` |
| Park on a spent tenant model budget, posted in the period alert's thread | Abandon | `repair` |
| Spent tenant model budget, one per budget period | Request increase, Acknowledge, Park all jobs, or Resume jobs while the jobs are parked | `repair` |
| `model_path_unavailable` | Acknowledge, Run validation, Fix done | `repair` |
| `run_failed`, `orphan_reclaimed`, `plan_rejected`, and `human_abandoned`, which follows an Abandon click | Acknowledge, Restart run | `repair` |

Retry repair first brings the agent branch onto the current head of the base branch. When the base moved, the base is merged into the branch and committed before the checks run again, so a fix pushed to the base reaches the retried run, and the later scans and commits compare against the new base. A conflict parks the run again with Retry repair, which syncs again after the conflict is resolved on either branch, and Abandon. A conflict only in a lockfile the profile regenerates takes the base copy, which the dependency step rewrites from the merged manifest.

A park on a decision carries what the decision is about in its detail. The `plan` park carries the plan, a paused review gate or a policy human review carries each gate's summary and finding titles, and a fail-closed review gate's alert carries the same for each blocking gate. The detail keeps its first and last lines within 40 lines and 4000 characters, with one note counting what it leaves out, and every credential-shaped value in it, such as an access key, a token, a private key, a URL password, or the value of an environment-style name ending in `PASSWORD`, `SECRET`, `TOKEN`, `API_KEY`, or `ACCESS_KEY`, reads `[masked]`.

Four gate parks stop a run at its commit, before anything is pushed. The `lockfile` park names the manifest, the lock command, and its output, and Approve commits the change without a regenerated lockfile unless the scan of that change also blocks, which parks the run on `dependency_scan` next. The `dependency_scan` park names each blocking advisory, package, and version the dependency step found. The `file_claim` park names a changed file that another active work unit of the tenant claims on the same repository, and Approve checks the claims again. The `workflow_files` park names each staged file under `.github/workflows/` or `.github/actions/` and carries its diff in the alert detail, because GitHub runs a pushed workflow with the repository's Actions secrets before any review. Approve commits and pushes the change, and a later commit that changes a workflow file parks again. Reject on any of the four abandons the run.

The `gate` kind admits clearing a named gate, and the `repair` kind admits every other button. A button records a decision or asks the deployment to check a condition, and never runs a command the click names. A configuration fault, including Pod Identity or Bedrock path drift in AWS, is fixed outside Slack by applying the declared configuration again, and is then confirmed with Run validation and Fix done. No button restarts LiteLLM.

| Button | What it does |
|---|---|
| Acknowledge | Records the Slack user on the alert and edits the post to show who acknowledged it and when |
| Run validation | Runs the tenant's deployment validation in the tenant's activity worker and posts the report in the alert's thread, naming each failing check. From the administrator channel, it runs the platform validation |
| Fix done | Probes the model path again. When the path answers, it closes the outage episode, edits the post to resolved, posts the recovery in the thread, triggers the tenant's intake schedule so the deferred tickets start, and resumes the parked runs. When the path still fails, it replies privately with the hop that fails, and the alert stays open |
| Restart run | Restarts the run as `inv restart` does, through the side-effect ledger keyed on the execution, so a double click or a later click restarts the run once. The post is edited to show who restarted it |
| Request increase | A select whose options are the amounts inside the platform band for the limit the run reached, a tenth of the configured limit, the step band of `LIMIT_GRANT_STEP_PERCENT`, and the remaining period band of `LIMIT_GRANT_PERIOD_PERCENT` when it is smaller. An option value carries only the amount, and the click is verified with the token of the Acknowledge button in the same post. The request reaches the administrators with Approve and Deny, and the reply names the outcome privately. An amount outside the band is refused with the statement that the configured limit must change |
| Park all jobs | On the period alert and on every run limit park. Records a pause for the tenant with the Slack user and the time, audited as `TENANT_JOBS_PAUSED`. While the pause is set, intake defers every ticket and running runs finish. The post is edited to show who parked the jobs, and Resume jobs takes the place of Park all jobs |
| Resume jobs | Clears the pause, audited as `TENANT_JOBS_RESUMED`, triggers the tenant's intake schedule, and edits the post to show who resumed the jobs, with Park all jobs in place of Resume jobs. Only Resume jobs clears a pause. A grant or the period reset leaves it in place |

The tenant model budget `TENANT_MODEL_BUDGET_USD` is spent when the spend of the tenant's LiteLLM team in the current period reaches the configured budget plus the period grants. While it is spent, intake defers every ticket, and a running run parks at the start of its next implementation, checks, or review stage. The first intake pass or parked run that finds the budget spent sends one alert, deduplicated through the side-effect ledger on the period and the effective budget that ran out, with Request increase over the amounts left in the period band, Acknowledge, and Park all jobs. A budget that runs out again after a period grant sends a new alert, and later parks reply in the new alert's thread. The ledger keeps each alert's Slack channel and `ts`. Each run that parks on the spent budget posts its park as a reply in that alert's thread, with its Abandon button, and posts no top-level message. A run that finds the alert still being posted waits up to 8 seconds for it, and a run that finds no Slack post of the alert posts at the top level and records why as `period_alert_fallback` on its `LIMIT_PARKED` audit event. The thread replies post fresh buttons before they expire and are edited to the decision as every park's posts are. An administrator's grant for the period edits the period alert to Granted and resumes the parked runs at once, and their thread replies show Granted. The first intake pass after the period reset resumes the parked runs, edits their thread replies and the last alert of the period to show that the budget period reset, replies in the alert's thread, and starts intake again.

An administrator's decision on a limit increase edits the administrator post and the tenant post the request was made on, and replies in the tenant post's thread. A grant resumes the parked runs first, then removes the tenant post's buttons and adds a line naming the approver. When a resume fails, the posts keep their buttons and the reply names the failure, so a second Approve click resumes the runs the first one missed. A denial adds a line naming the denier, leaves the run parked, and keeps the Abandon button. A limit park posts fresh buttons before they expire as every decision park does, and an expired click on Request increase or Acknowledge posts them too.

The model path outage post also resolves without a click. The intake pass whose probe answers again closes the episode, edits the tenant post and the administrator post to resolved, and replies in each thread with the recovery. Whichever source resolves the outage, the administrators receive one recovery message.

Each applied action writes a `SLACK_ALERT_ACTION` audit event with the actor, the action, and the basis of the approval. A refused click writes a `SLACK_ACTION_REFUSED` event, and the ephemeral reply names the reason. Every refusal of a park click applies to an alert click as well, and an alert click adds these.

| Reason | Cause | Reply |
|---|---|---|
| `resolved` | The outage episode the button names is closed | This alert is already resolved |
| `earlier_post` | The post is not the one the open outage episode recorded | The message belongs to an earlier alert of this outage |
| `already_restarted` | The run was already restarted from this alert | This run was already restarted from this alert |
| `not_permitted` | The button names an action the alert does not offer | This action is not available for this run |

## Alert audiences

An alert goes to whoever can act on it. An administrator issue needs platform access, and these are the model path, Temporal, the database, a defect in ADP that parks a run, sandbox and gate infrastructure, platform configuration, limit changes, and platform-scope validation failures. Every other issue is the tenant's, including the restart of a run.

| Audience | Alerts | The tenant receives | The administrators receive |
|---|---|---|---|
| Platform | `model_path_unavailable`, an `awaiting_fix` park, a `model_unavailable` park, and a failed platform check of the deployment validation | The effect and the statement that work resumes on its own, with no fix steps and no platform internals | The error, the fix for the deployment posture, and how to validate it |
| Tenant and administrators | `run_failed` on a platform error type, such as a Temporal RPC error, a database connection error, an unreachable LiteLLM, or an unavailable sandbox | The full alert with Acknowledge and Restart run | The cause, without fix steps |
| Tenant | Every other alert | The full alert with its buttons | Nothing |

The `model_path_unavailable` post to the tenant holds what happened, the effect, the status, and how to validate, with Acknowledge, Run validation, and Fix done. The administrator post holds the error, how to fix it, and how to validate it, with the same buttons. A tenant configuration fault names the file and key to change in the tenant's config repository.

## Platform administrators

`config/platform/platform-escalation.yaml`, copied from [`platform-escalation.yaml.example`](../config/platform/platform-escalation.yaml.example), names the administrator destination. A deployed process reads it from the directory `PLATFORM_ENV_CONFIG_DIR` names when that directory holds it, and otherwise from `config/platform/` of the image.

```yaml
config_version: 1
administrators:
  slack:
    channel: C0123456789
    allowlist:
      repair:
        - user_id: U0123456789
        - group_id: S0123456789
  email:
    - platform-admins@example.com
```

| Key | Meaning |
|---|---|
| `administrators.slack.channel` | The administrator channel ID, which starts with `C`. The shared Slack app posts to it through the posting service, so the key requires `slack-shared-app.yaml` in the platform directory and the shared bot in the channel |
| `administrators.slack.allowlist` | Who may press the buttons of an administrator post, under the `repair` kind, in the `channel`, `user_id`, and `group_id` forms of [Approvers](#approvers). It defaults to `repair: channel`, which admits anyone in the administrator channel and the shared app's organization |
| `administrators.email` | The administrator addresses. Each address needs a domain with a dot, such as `admins@platform.test`. The sender sends over its `SMTP_HOST`, `SMTP_PORT`, and `ALERT_EMAIL_FROM` |

With only `email` set, the administrators receive email only. With only `slack.channel` set, they receive Slack only. With both set, they receive both. `inv config-check`, every activity worker at start, and the posting service at start refuse a file that names neither, a missing file, and a channel without the shared app. The tenant activity workers send the administrator alerts of their tenant, and the `deployment-validate` Job sends the failed platform checks, as the platform caller for Slack.

An administrator click arrives on the shared app's route of the inbound webhook, at `/webhook/slack/_shared` or over its Socket Mode connection. The webhook recognizes the administrator channel, reads the tenant the button's token names, and has that tenant's dispatcher verify the token under the tenant's action key. The dispatcher admits the user under the administrator allowlist, whose groups it reads under the scope `_platform`, and a refused click writes a `SLACK_ACTION_REFUSED` event with `actor_type` `slack_admin`. The webhook holds no tenant credential, so it hands an admitted click to the tenant's activity worker as an `AlertActionWorkflow` on the tenant's task queue, and the worker takes the action. The click is answered with the action's outcome when it arrives within 2.5 seconds. Otherwise the answer states that the worker is taking the action, or, when no worker has started it, as while the tenant worker restarts, that the action has not been picked up and the button can be pressed again. Fix done from the administrator channel resolves the tenant's outage episode, which edits both posts and resumes the tenant's work.

## Network

The DNS Firewall allows `hooks.slack.com` by default. A deployment that uses the Web API adds `slack.com`, and a GovSlack deployment adds `slack-gov.com` and `hooks.slack-gov.com`, through `dns_firewall_additional_domains`. Socket Mode connects to the WebSocket host `apps.connections.open` returns, which release verification records for each deployment, from the tenant's activity worker for a tenant's own app and from the inbound webhook for the shared app. Slack sends request URL clicks from unpublished addresses, so the webhook relies on the signature rather than an address allowlist. With the shared app, the `slack-poster` pod calls the Web API, and the `slack-poster-ingress` NetworkPolicy admits only tenant activity worker pods and the `inbound-webhook` and `deployment-validate` pods to its port. Both environments apply the policy, and a local cluster enforces it only when its network plugin enforces NetworkPolicy.

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
