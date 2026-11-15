---
title: Slack Escalation Channel Plan
type: feat
date: 2026-11-06
origin: Jira ADP-160
ticket: ADP-160
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: jira
execution: local-stack
---

# Slack Escalation Channel Plan

## Goal Capsule

Objective. A run that parks for a human reaches the responsible operator in Slack, and the operator resolves it from the message. The same code serves private deployments on commercial Slack, state deployments on commercial Slack or Enterprise Grid, and federal deployments on GovSlack.

Means. Put the Jira comment, Slack, and email deliveries behind an escalation channel interface. Add a Slack adapter on the Web API with a configurable API host that posts Block Kit messages with signed action buttons. Accept the button clicks through an HTTPS request URL on the inbound-webhook service or through a Socket Mode worker, selected per deployment, and resolve them through the existing gate and repair decision paths.

Product authority. Jira ADP-160 under the v0.3.0 Epic ADP-48. ADP-137 provides the EKS deployment that carries the activity worker and inbound-webhook in AWS. ADP-24 asks how an alert remedy stays runnable where the operator reads it, and a Slack action answers that for this channel by executing the remedy inside the platform.

Stop conditions. A run parked at a human gate and a run parked for repair are each resolved from a Slack message in webhook mode and in Socket Mode, with the Slack identity recorded as the actor. Pointing the API host at `slack-gov.com` routes every call there. The `government` profile refuses an unapproved Slack host and defaults to minimal message content. Jira comment and email delivery are unchanged when Slack is not configured. `inv ci` passes.

## Product Contract

### Summary

`services/orchestration/notifications.py` fans an alert out to a Jira comment, a Slack incoming webhook, and SMTP email. `alert_human_intervention` in `services/orchestration/temporal_activities/notify.py` calls it when `DraftPullRequestWorkflow` parks at `AWAITING_HUMAN` or stops, and intake calls it when a ticket cannot be routed. The message is plain text with the repository, task, execution, workflow, stage, reason, and failure detail. The operator resolves a parked run with `inv gate` or `inv repair`, which call `run_gate_decision` and `run_repair_decision` with the operating-system account as the actor. Those tasks need a host checkout, because the platform image does not contain `tasks.py`.

`slack_webhook_url` is defined in the secret store, but no Kubernetes manifest or ECS service definition passes it to a service. The DNS Firewall allowlist contains `hooks.slack.com` and no other Slack name, so the Web API, Socket Mode, and GovSlack are unreachable from a deployed VPC.

The platform serves private, state, and federal deployments. GovSlack runs on the separate domain `slack-gov.com`, holds a FedRAMP High authorization, and requires every Web API call, OAuth call, `response_url`, and request URL to use that domain. Its apps use granular bot scopes and OAuth v2, and an app manifest carries `socket_mode_enabled` and compliance values.

### Requirements

- R1. An escalation channel interface delivers an alert and reports whether it delivered. Jira comment, Slack, and email are channels. `config/<env>/escalation.yaml` selects the enabled channels, and a deployment without the file keeps the current behaviour, with each channel enabled when its settings are present.
- R2. The Slack channel posts through the Web API with a bot token. Its API base URL is configurable and defaults to `https://slack.com/api/`, so `https://slack-gov.com/api/` serves GovSlack with no code change. A `webhook_only` mode keeps the incoming webhook for one-way delivery with no actions.
- R3. A human-gate park posts approve and reject actions. A repair park posts retry and abandon actions. Other alerts post no actions and link to the ticket.
- R4. Each action carries a token signed with HMAC-SHA256 under a platform key. The token binds the workflow ID, the issue key, the decision kind, the gate name, the permitted actions, an expiry, and a nonce. A missing, altered, or expired token changes nothing.
- R5. The inbound-webhook service accepts Slack interactivity at `/webhook/slack`. It verifies the `v0` signature over `v0:<timestamp>:<body>` with the signing secret in constant time, rejects a timestamp more than five minutes from the server clock with 401, and parses the form-encoded `payload` field.
- R6. A Socket Mode worker opens the WebSocket URL that `apps.connections.open` returns for an app-level token, acknowledges each envelope, and hands the action to the same dispatcher as R5.
- R7. `escalation.yaml` sets the Slack transport to `webhook`, `socket`, or `hybrid`, following the integration modes in `config/<env>/integrations.yaml`. Each transport refuses to start without its credential.
- R8. An action resolves only when the Slack team ID and user ID appear in the allowlist for that decision kind. The dispatcher confirms the workflow is still parked for that issue and gate before signalling. The actor recorded in the workflow signal, the audit event, and the evidence record is `slack:<team_id>/<user_id>`.
- R9. Each action runs once. The dispatcher records it in the side-effect ledger keyed on the token nonce, so a retried delivery, a double click, or a webhook and socket delivery of the same click resolves the run once.
- R10. After an action, the message is updated to show the outcome and the actor, with its buttons removed. A refused action receives an ephemeral reply that states the reason.
- R11. Message content has two levels. `full` carries the failure detail. `minimal` carries the issue key, the stage, the reason code, and a link to the ticket. The `government` gates profile defaults to `minimal`.
- R12. Under the `government` profile, the Slack API host must appear in `approved_hosts` in `escalation.yaml`. `inv config-check` reports a host outside the list, and the activity worker, inbound-webhook, and Socket Mode worker refuse to start with it.
- R13. The secret store holds `slack_bot_token`, `slack_signing_secret`, `slack_app_token`, and a generated `escalation_action_key`. The local Kubernetes secret generation and Compose supply the same values. The DNS Firewall allowlist takes the Slack hosts the deployment uses through `additional_allowed_domains`.
- R14. `config/platform/slack-app-manifest.yaml.example` declares the bot scopes, the interactivity request URL, `socket_mode_enabled`, and the GovSlack compliance block.
- R15. The FedRAMP hardening design and the control inheritance matrix record AC-3, IA-2, AU-2, AU-12, SC-7, and SC-8 for the channel.

### Scope Boundaries

- The EKS cluster, its service accounts, and its ingress are ADP-137. This plan adds the Kubernetes manifest changes and the environment wiring that ADP-137 deploys.
- Remedy text for the Jira comment and email channels stays with ADP-24.
- Microsoft Teams and other chat platforms are not built here. The channel interface admits them later.
- Conversational commands, slash commands, and status queries from Slack are out of scope.
- Distribution through the Slack Marketplace or the GovSlack Marketplace is out of scope. Each deploying organization creates and installs its own app from the manifest template.

### Sources

- Slack developer documentation for GovSlack at `docs.slack.dev/govslack`, covering the `slack-gov.com` domain, the FedRAMP High authorization, and the manifest compliance values.
- Slack developer documentation for request signing, interactivity payloads and `response_url`, `chat.postMessage`, `chat.update`, `chat.postEphemeral`, Socket Mode, and `apps.connections.open`.

## Planning Contract

### Key Technical Decisions

- The Slack adapter uses `slack_sdk`, whose `WebClient` accepts a `base_url`, whose `SocketModeClient` covers R6, and whose `SignatureVerifier` covers R5. The package has no required transitive dependencies. Each client is constructed with the configured base URL, so no `slack.com` literal remains in the code path.
- `IntegrationHandler` in `services/orchestration/webhook/inbound.py` gains a `decode` step that defaults to the current JSON parse. The Slack handler overrides it to read the form-encoded `payload` field. `handle_inbound` keeps its verify, allowlist, ledger, and dispatch order.
- The dispatcher is a module that both transports call. It verifies the token, checks the allowlist, confirms the workflow is parked, signals through workflow-scoped variants of `resume_gate` and `resume_repair`, and records the audit event. A workflow-scoped variant signals only the workflow in the token, which differs from `inv gate`, which signals every parked workflow for the issue.
- The transport acknowledges inside Slack's three-second window and updates the message through `response_url` after the signal returns, so a slow Temporal call does not cause Slack to retry.
- The token is a compact HMAC-signed JSON value carried in the button `value`, under Slack's 2,000-character limit. The default expiry is 72 hours and is configurable. The key is generated by the secret store so that it rotates with the other generated secrets.
- The Socket Mode worker runs as its own process with a heartbeat file, matching the Temporal workers, so its health is visible to the existing health checks.
- `minimal` content omits failure output because it can carry file paths, stack traces, and repository content, which a deployment may not permit outside its boundary.
- The `government` profile check reads the gates profile already loaded from `gates.yaml`, so a deployment declares its tier once.

### Sequencing

U1, then U2 and U3, then U4 and U5 in parallel, then U6, U7, U8, and U9.

## Implementation Units

### U1. Escalation channel interface and configuration

Add the channel protocol, the registry, and the `escalation.yaml` loader with `config_version`, the enabled channels, the Slack settings, the transport, the content level, the allowlists, and `approved_hosts`. Move the three deliveries in `notifications.py` behind it. Add `escalation.yaml.example` for `local` and report the file in `inv config-check`. Tests cover the default when the file is absent and each validation error.

### U2. Slack Web API adapter and messages

Add the adapter with the configurable base URL, Block Kit rendering for gate, repair, and informational alerts at both content levels, and the `webhook_only` mode. Tests assert the request host for `slack.com` and `slack-gov.com`, the blocks per alert kind, and the omission of detail at `minimal`.

### U3. Action tokens and dispatcher

Add token signing and verification, the workflow-scoped gate and repair resume functions, the allowlist check, the ledger record keyed on the nonce, the audit event, and the message update. Tests cover an altered, expired, and replayed token, a user outside the allowlist, a workflow no longer parked, and a successful decision of each kind.

### U4. HTTPS request URL transport

Add the Slack handler and the `/webhook/slack` route, the `decode` step, the signature and timestamp checks, and the signing secret in `_INBOUND_SECRETS`. Tests cover a valid request, an invalid signature, a stale timestamp, and a form body.

### U5. Socket Mode transport

Add the Socket Mode worker, its entrypoint, its heartbeat, and its Compose service under a profile. Tests drive it with a fake socket client and assert that an envelope reaches the dispatcher and is acknowledged.

### U6. Profile and host enforcement

Add the `government` profile default for content level and the `approved_hosts` check at startup in the three services and in `inv config-check`. Tests cover an approved host, an unapproved host, and the `private` profile.

### U7. Secrets, network, and deployment wiring

Add the three Slack secrets and the generated action key to the secret-store module and its README table. Add them to the local Kubernetes secret generation, the `inbound-webhook` manifest, and the Compose environment. Document the Slack hosts to add through `additional_allowed_domains` for commercial Slack and GovSlack. Terraform tests assert the new secrets and their read policies.

### U8. Slack app manifest and documentation

Add `config/platform/slack-app-manifest.yaml.example`. Document app creation on `api.slack.com/apps` and `api.slack-gov.com/apps`, the transports, the allowlists, and the content levels in the README. Add the controls to the hardening design and the control inheritance matrix, and add the CHANGELOG entry.

### U9. Verification

Run the acceptance criteria against the local stack and a Slack test workspace in webhook mode and in Socket Mode through an environment-gated smoke test, then run `inv ci`.

## Open Questions

- Whether each deploying organization may install a GovSlack app built from the manifest without listing it in the GovSlack Marketplace. The scope boundary assumes it can.
- Which Slack hosts the Socket Mode WebSocket resolves to on GovSlack, which sets the DNS Firewall entries for a federal deployment.
