# Roadmap

This roadmap groups the remaining platform work into versions. The authoritative, code-verified feature list and per-feature detail live in the [remaining-work roadmap plan](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md). Each version below names its theme, its goal, and its features with an effort estimate in hours of uninterrupted work. Effort ranges carry the confidence the source plan recorded.

Every v0.2.0 feature and every v0.3.0 feature planned before the Jira board has a dedicated implementation plan. The v0.3.0 features added from the Jira board name their ticket. Later versions link to their entry in the roadmap plan until their plans are written.

Total remaining scope is roughly 153 to 203 hours across 22 features.

## v0.1.0 Pluggable ticket and source-control backends

Status. Shipped. Delivered as commits ADEVX-95, ADEVX-96, and ADEVX-97.
Goal. A deployment can track work in a non-Jira tracker and target a non-GitHub source control, and a deployment that configures neither behaves exactly as today. This version also stands up the rebar S3 and MinIO object store that later versions build on, bounds concurrent runs with a platform admission cap, and re-grounds a run when it discovers a cross-ticket dependency during planning.

| Feature | Effort | Detail |
|---|---|---|
| Pluggable ticket-tracker and source-control backends | 32-48h (low) | [plan](docs/plans/2026-09-02-2022-feat-pluggable-ticket-source-control-backends-plan.md) |
| Cross-ticket dependency discovery and re-plan | 16-24h (low) | [plan](docs/plans/2026-09-03-1417-feat-cross-ticket-dependency-discovery-plan.md) |
| Cross-ticket dependency discovery on Jira | 12-20h (low) | [plan](docs/plans/2026-09-11-1410-feat-jira-dependency-backend-plan.md) |

## v0.2.0 rebar review integration

Status. Shipped. Delivered as commits ADEVX-99, ADEVX-100, ADEVX-101, and ADEVX-103.
Goal. The rebar review gates run on the native Bedrock path with per-leg failover and signed verdicts, and the review path's remaining rebar-dependent gaps are closed. Authored-identity signing depends on the object store introduced in v0.1.0.

| Feature | Effort | Detail |
|---|---|---|
| Native Bedrock review path | 6h | [plan](docs/plans/2026-09-02-2042-feat-native-bedrock-review-path-plan.md) |
| Signed review verdicts | 8h | [plan](docs/plans/2026-09-11-2100-feat-signed-review-verdicts-plan.md) |
| Acceptance-gate live validation | 3-6h (medium) | [plan](docs/plans/2026-09-11-2110-feat-acceptance-gate-live-validation-plan.md) |
| Authored-identity signing | 8-16h (low) | [plan](docs/plans/2026-09-11-2120-feat-authored-identity-signing-plan.md) |

Version effort. 25-36h.

Sequencing. Native Bedrock review path first, since it settles the review path and stands up the dedicated review worker the later features build on. Signed review verdicts next, mounting its signing key on that review worker. Acceptance-gate live validation after, so it validates the settled review path. Authored-identity signing last, since it carries the most uncertainty and gates on its own coupling evaluation.

## v0.3.0 AWS transition and FedRAMP readiness

Status. In progress. v0.3.0 is the FedRAMP Medium readiness release and does not ship until the platform-owned technical controls are met. The hosting account holds a FedRAMP Medium authorization, so the platform inherits the controls AWS owns and implements the configuration and provisioning controls on top. The scope was expanded from a design-only release to provision the production infrastructure the platform-owned controls require. The local-to-AWS mapping, the schema contracts, the WORM evidence and audit store, the full evidence artifact coverage, the hardening design, the data tier with its tiered network, egress path, and remote state, the secret store with per-service database roles and credential rotation, and the rebar ticket store bucket are delivered.
Goal. The platform is technically FedRAMP Medium ready. It has documented local-to-AWS mappings, a control inheritance and shared-responsibility matrix, committed audit and evidence contracts, an S3-backed WORM evidence and audit store, and terraform that provisions the data tier, secret store, container-platform services, Temporal control plane, observability, and the rebar ticket bucket with encryption, least privilege, backups, and private networking, plus a hardened AWS security baseline. Organizational and operational control families remain the deploying organization's responsibility, recorded in the inheritance matrix.

| Feature | Effort | Detail |
|---|---|---|
| Local-to-AWS mapping document | 2-3h | [plan](docs/plans/2026-09-14-1855-feat-local-to-aws-mapping-plan.md) |
| Audit and evidence schema contracts | 3-4h | [plan](docs/plans/2026-09-14-1856-feat-audit-evidence-schema-contracts-plan.md) |
| S3-backed WORM evidence and audit store | 8-10h | [plan](docs/plans/2026-09-14-1857-feat-s3-worm-evidence-audit-store-plan.md) |
| FedRAMP Medium hardening design | 6-8h | [plan](docs/plans/2026-09-14-1858-feat-fedramp-medium-hardening-design-plan.md) |
| FedRAMP control inheritance and shared-responsibility matrix | 4-6h | [plan](docs/plans/2026-09-14-2145-feat-fedramp-control-inheritance-matrix-plan.md) |
| Evidence retention configuration | 2h | [plan](docs/plans/2026-09-14-1859-feat-evidence-retention-configuration-plan.md) |
| Evidence retention classification mapping | 3-4h | [plan](docs/plans/2026-09-14-2147-feat-evidence-retention-classification-mapping-plan.md) |
| AWS security baseline hardening | 4-6h | [plan](docs/plans/2026-09-14-2146-feat-aws-security-baseline-hardening-plan.md) |
| Data and cache tier terraform | 24-32h | [plan](docs/plans/2026-09-14-2148-feat-data-cache-tier-terraform-plan.md) |
| Secret store terraform | 8-13h | [plan](docs/plans/2026-09-14-2149-feat-secret-store-terraform-plan.md) |
| Rebar ticket store bucket terraform | 16-21h | [plan](docs/plans/2026-09-14-2150-feat-rebar-ticket-store-bucket-terraform-plan.md) |
| ECS Fargate services for the platform containers | 20-30h (low) | Jira ADP-136, a sub-task of ADP-61. [plan](docs/plans/2026-09-14-2151-feat-container-platform-services-terraform-plan.md) |
| EKS cluster for the pod-orchestrating workloads | 28-40h (low) | Jira ADP-137, a sub-task of ADP-61. [plan](docs/plans/2026-09-14-2151-feat-container-platform-services-terraform-plan.md) |
| Local Temporal server upgrade to 1.32.1 | 6-10h | Jira ADP-138. [plan](docs/plans/2026-10-30-1330-feat-temporal-server-upgrade-plan.md) |
| Temporal control plane terraform | 10-14h | Jira ADP-97. [plan](docs/plans/2026-09-14-2152-feat-temporal-control-plane-terraform-plan.md) |
| Observability terraform | 6-10h | Jira ADP-95. [plan](docs/plans/2026-09-14-2153-feat-observability-terraform-plan.md) |
| Activity worker, LiteLLM, and OPA in the local cluster | 8-12h | Jira ADP-139. Also removes `langgraph-service`. |
| Private-endpoint jump-host residual hardening | 1-2h | Jira ADP-104. [plan](docs/plans/2026-09-14-1900-feat-jumphost-residual-hardening-plan.md) |
| Full evidence artifact coverage | 4-6h | [plan](docs/plans/2026-09-14-1901-feat-full-evidence-artifact-coverage-plan.md) |
| Remaining connectivity and sandbox confirmations | 3-6h (low) | Jira ADP-92. [plan](docs/plans/2026-09-14-1902-feat-connectivity-sandbox-confirmations-plan.md) |
| MCP gateway client authentication | 3-5h | Jira ADP-140 |
| Leaked OpenHands sandbox pods | 3-5h | Jira ADP-141 |

Version effort. 172-249h.

## v0.4.0 Production serving and demo readiness

Status. Proposed. Ordering after v0.3.0 is a suggestion, not a dependency.
Goal. The repository-intelligence service runs behind a production server boundary, prompts and database fixtures are committed and seedable, model calls carry their attribution headers and usage through rebar, and the JavaScript read path is proven with a dedicated corpus.

| Feature | Effort | Detail |
|---|---|---|
| Repository-intelligence production server boundary | 12h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Prompt centralization | 4h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Native rebar request headers | 3h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Planner on the rebar runner | 6h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Database seeds and reset | 5h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| JavaScript SCIP verification corpus | 6h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Remove unwired feature code | 3h | Remove the functions that no runtime path calls, with their tests and docs: `rotate_identity_key` in `services/orchestration/authorship.py`, `should_resume` and `select_resumable` in `services/orchestration/suspension.py`, `available_models_from_litellm` in `services/orchestration/gates/model_resolver.py`, `ensure_status` and `post_comment` in `services/integration/jira/writes.py`, `webhook_enabled` in `services/integration/integrations.py`, `admit_sequence` in `services/orchestration/coordination_client.py`, `active_key_ids` in `services/review/trust_root.py`, `repo_search_structural` in the ast-grep provider, and `suspend_for` in `services/orchestration/temporal_workflows/suspendable.py`. |
| Raise the rebar floor for the split-remote repair | 1h | Once a rebar release resolves the multi-bundle repair remote with `git remote get-url --push` ([navapbc/rebar#145](https://github.com/navapbc/rebar/issues/145)), raise the `nava-rebar` pin to it and remove the manual repair steps from the `rebar-ticket-store` README. |
| Remove the cache snapshot compatibility flag | 1h | Drop `--rdb-version-check relaxed` from the `redis` service in `compose.yaml`. It exists only so Valkey can load a Redis 7.4 snapshot on the first start after upgrading to v0.3.0, and every local cache volume is in the Valkey format after that start. Remove the matching README and CHANGELOG notes. |

Version effort. 41h.

## v0.5.0 Runtime robustness and interactivity

Status. Proposed. Ordering after v0.4.0 is a suggestion, not a dependency.
Goal. A material Jira change during a run pauses at a drift checkpoint, interactive surfaces render streamed responses, and the review finder is tuned for cost.

| Feature | Effort | Detail |
|---|---|---|
| Snapshot-drift handling | 14h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Streaming inference and interactive surfaces | 6h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Single-turn finder tuning | 4-8h (medium) | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |

Version effort. 24-28h.

## Held, blocked on an external litellm release

These items unblock on a litellm release that the `openhands-sdk` version range accepts. The first needs [BerriAI/litellm#44178](https://github.com/BerriAI/litellm/pull/44178) or an equivalent fix, and the second needs litellm to allow filelock 4.

- Raise the litellm floor to the fixed release. litellm declares `ReadOnly` items on TypedDict classes such as `OpenAIRealtimeInputAudioBufferSpeechEvent`, and pydantic emits a `UserWarning` when the OpenHands SDK imports litellm and builds their schemas. Confirm the warning is gone from `inv ci` output after the upgrade.
- Remove the `filelock>=4.2.1` override from `[tool.uv]` once a litellm release that the `openhands-sdk` range accepts allows filelock 4. Confirm with `uv lock` that filelock stays at 4.2.1 or newer without the override.

## Source of truth

Feature status in the roadmap plan is verified against committed code, not plan prose. When a feature ships, its status is derived from git rather than edited here. This file groups and sequences the work. The roadmap plan and the per-feature implementation plans under [docs/plans/](docs/plans) carry the detail.
