# Slack escalation

When a run parks for a human, the activity worker of the run's tenant posts the alert to the tenant's Slack channel. A gate park carries Approve and Reject buttons, a repair park after exhausted repairs carries Retry repair and Abandon, and a park on a spent run or tenant model budget carries Abandon only. A click resolves the park as `inv gate`, `inv repair --retry`, or `inv repair --abandon` would, and records the Slack user as the actor. Every message keeps the `Next step:` line, so the command still works for an operator who does not use Slack.

The same message reaches commercial Slack, Enterprise Grid, and GovSlack. The Jira comment and the alert email are delivered before Slack, and a Slack failure leaves `slack` out of the alert's channels without failing the park.

## One app per tenant

Each tenant creates its own internal Slack app from [`config/platform/slack-app-manifest.yaml.example`](../config/platform/slack-app-manifest.yaml.example), at `https://api.slack.com/apps` for commercial Slack or `https://api.slack-gov.com/apps` for GovSlack. No two tenants share an app, a token, a signing secret, or an action key, and the render and the inbound webhook refuse a configuration in which they do.

| Credential | Setting | Read by |
|---|---|---|
| Bot token, `xoxb-` | `SLACK_BOT_TOKEN` | The tenant's activity worker, which posts and updates messages |
| App-level token, `xapp-` with `connections:write` | `SLACK_APP_TOKEN` | The tenant's activity worker, for Socket Mode |
| Signing secret | `SLACK_SIGNING_SECRET`, delivered to the webhook as `SLACK_SIGNING_SECRET_<TENANT>` | The inbound webhook, for request URL clicks |
| Action key | `ESCALATION_ACTION_KEY`, delivered to the webhook as `ESCALATION_ACTION_KEY_<TENANT>` | The tenant's activity worker, which signs the buttons, and the inbound webhook |

Locally the home tenant sets these in `.env` and every other tenant in `config/local/tenants/<tenant>.env`. Generate the action key with `openssl rand -hex 32`. In AWS the bot and app tokens are the tenant's `slack_bot_token` and `slack_app_token` secrets, the signing secret is its `slack_signing_secret` webhook secret, and the tenant module generates `escalation_action_key`, which only the tenant's worker role and the webhook reader read.

With a bot token and a `slack` block in `escalation.yaml`, the bot posts and `SLACK_WEBHOOK_URL` stays unused. Without a bot token, `SLACK_WEBHOOK_URL` posts the alert text with no buttons, as before.

### Creating the app

Creating and installing an app needs that right in the workspace. A member without it asks a workspace admin to create the app and add them under **Basic Information**, **Collaborators**, or tests in a workspace they own. Every page named below is in the left sidebar of the app's settings, which open from the app's name at `https://api.slack.com/apps`, or `https://api.slack-gov.com/apps` for GovSlack.

The steps copy two kinds of value. The tokens and the signing secret are secrets and go in the tenant's env file, `.env` for the home tenant or `config/local/tenants/<tenant>.env` for another tenant. The IDs named `slack.<key>` are not secrets and go under `slack:` in the tenant's `escalation.yaml`, `config/local/escalation.yaml` for the home tenant or `config/local/tenants/<tenant>/escalation.yaml` for another tenant.

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

## escalation.yaml

The file sits in the tenant's config bundle, `config/<environment>/tenants/<tenant>/escalation.yaml` locally or the bundle in the tenant config bucket, and `config/<environment>/escalation.yaml` serves a single-tenant workstation. A tenant without the file delivers to every channel whose settings are present.

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

The shared inbound webhook accepts each tenant's clicks at `/webhook/slack/<tenant>`. It refuses a missing, non-numeric, or more than five minutes skewed timestamp and an invalid `v0` signature with 401 before it reads the body, checks the app ID, applies the decision within two seconds, and responds. It then updates the message through the click's `response_url`, which it calls only over HTTPS on the `/actions/` path of `hooks.slack.com`, or of `hooks.slack-gov.com` for a GovSlack tenant, without following redirects. The webhook holds no bot token. It reads each tenant's `escalation.yaml` and `gates.yaml` from the tenant's bundle at startup and again on a background thread every `TENANT_CONFIG_REFRESH_SECONDS`, so a click never waits on a bundle read. A file that fails validation leaves the last valid settings in force.

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

The DNS Firewall allows `hooks.slack.com` by default. A deployment that uses the Web API adds `slack.com`, and a GovSlack deployment adds `slack-gov.com` and `hooks.slack-gov.com`, through `dns_firewall_additional_domains`. Socket Mode connects to the WebSocket host `apps.connections.open` returns, which release verification records for each deployment. Slack sends request URL clicks from unpublished addresses, so the webhook relies on the signature rather than an address allowlist.

## When a credential leaks

A leaked signing secret or app-level token lets its holder act as any allowlisted user, or receive clicks on a connection of their own.

1. Regenerate the signing secret, or revoke the app-level token and create another, in the app's settings.
2. Write the new value to `.env` or the tenant's settings file and run `inv k8s-reload`, or update the tenant's secret in Secrets Manager and roll the inbound webhook or the tenant's activity worker.
3. Rotate `ESCALATION_ACTION_KEY` the same way. Every button already posted stops verifying, and its `Next step:` command still works.
4. Review the `SLACK_ACTION_REFUSED` and `HUMAN_INTERVENTION_DECISION` audit events since the leak.
