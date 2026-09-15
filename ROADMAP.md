# Roadmap

This roadmap groups the remaining platform work into versions. The authoritative, code-verified feature list and per-feature detail live in the [remaining-work roadmap plan](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md). Each version below names its theme, its goal, and its features with an effort estimate in hours of uninterrupted work. Effort ranges carry the confidence the source plan recorded.

Every v0.2.0 and v0.3.0 feature has a dedicated implementation plan. Later versions link to their entry in the roadmap plan until their plans are written.

Total remaining scope is roughly 82 to 97 hours across 15 features.

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

Status. Planned. The per-feature effort was re-verified against the current codebase. The private-endpoint jump-host image, the Fargate module, and the connectivity provider shipped in v0.1.0, so that feature is reduced to residual image and role hardening.
Goal. The platform has documented local-to-AWS mappings, committed audit and evidence contracts, an S3-backed WORM evidence and audit store, a FedRAMP Medium hardening design, and the connectivity a deployed environment needs. The WORM store shares the object store stood up in v0.1.0.

| Feature | Effort | Detail |
|---|---|---|
| Local-to-AWS mapping document | 2-3h | [plan](docs/plans/2026-09-14-1855-feat-local-to-aws-mapping-plan.md) |
| Audit and evidence schema contracts | 3-4h | [plan](docs/plans/2026-09-14-1856-feat-audit-evidence-schema-contracts-plan.md) |
| S3-backed WORM evidence and audit store | 8-10h | [plan](docs/plans/2026-09-14-1857-feat-s3-worm-evidence-audit-store-plan.md) |
| FedRAMP Medium hardening design | 6-8h | [plan](docs/plans/2026-09-14-1858-feat-fedramp-medium-hardening-design-plan.md) |
| Evidence retention configuration | 2h | [plan](docs/plans/2026-09-14-1859-feat-evidence-retention-configuration-plan.md) |
| Private-endpoint jump-host residual hardening | 3h | [plan](docs/plans/2026-09-14-1900-feat-jumphost-residual-hardening-plan.md) |
| Full evidence artifact coverage | 4-6h | [plan](docs/plans/2026-09-14-1901-feat-full-evidence-artifact-coverage-plan.md) |
| Remaining connectivity and sandbox confirmations | 3-6h (low) | [plan](docs/plans/2026-09-14-1902-feat-connectivity-sandbox-confirmations-plan.md) |

Version effort. 31-42h.

## v0.4.0 Production serving and demo readiness

Status. Proposed. Ordering after v0.3.0 is a suggestion, not a dependency.
Goal. The repository-intelligence service runs behind a production server boundary, prompts and database fixtures are committed and seedable, and the JavaScript read path is proven with a dedicated corpus.

| Feature | Effort | Detail |
|---|---|---|
| Repository-intelligence production server boundary | 12h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Prompt centralization | 4h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Database seeds and reset | 5h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| JavaScript SCIP verification corpus | 6h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |

Version effort. 27h.

## v0.5.0 Runtime robustness and interactivity

Status. Proposed. Ordering after v0.4.0 is a suggestion, not a dependency.
Goal. A material Jira change during a run pauses at a drift checkpoint, interactive surfaces render streamed responses, and the review finder is tuned for cost.

| Feature | Effort | Detail |
|---|---|---|
| Snapshot-drift handling | 14h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Streaming inference and interactive surfaces | 6h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Single-turn finder tuning | 4-8h (medium) | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |

Version effort. 24-28h.

## Held, blocked on external rebar 0.13

These are not scheduled into a version. They unblock when rebar 0.13 ships the request-header seam and the OpenAI-path usage fix, verified against the unblock rule in the roadmap plan.

- Drop the process-wide httpx transport header workaround in favor of a native rebar request-header seam.
- Migrate the planner from its raw httpx LiteLLM client onto the rebar runner.

## Source of truth

Feature status in the roadmap plan is verified against committed code, not plan prose. When a feature ships, its status is derived from git rather than edited here. This file groups and sequences the work. The roadmap plan and the per-feature implementation plans under [docs/plans/](docs/plans) carry the detail.
