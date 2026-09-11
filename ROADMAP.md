# Roadmap

This roadmap groups the remaining platform work into versions. The authoritative, code-verified feature list and per-feature detail live in the [remaining-work roadmap plan](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md). Each version below names its theme, its goal, and its features with an effort estimate in hours of uninterrupted work. Effort ranges carry the confidence the source plan recorded.

Two features have dedicated implementation plans already. The rest link to their entry in the roadmap plan.

Total remaining scope is roughly 163 to 207 hours across 21 features.

## v0.1.0 Pluggable ticket and source-control backends

Status. Next up. The implementation plan is ready.
Goal. A deployment can track work in a non-Jira tracker and target a non-GitHub source control, and a deployment that configures neither behaves exactly as today. This version also stands up the rebar S3 and MinIO object store that later versions build on, bounds concurrent runs with a platform admission cap, and re-grounds a run when it discovers a cross-ticket dependency during planning.

| Feature | Effort | Detail |
|---|---|---|
| Pluggable ticket-tracker and source-control backends | 32-48h (low) | [plan](docs/plans/2026-09-02-2022-feat-pluggable-ticket-source-control-backends-plan.md) |
| Cross-ticket dependency discovery and re-plan | 16-24h (low) | [plan](docs/plans/2026-09-03-1417-feat-cross-ticket-dependency-discovery-plan.md) |
| Cross-ticket dependency discovery on Jira | 12-20h (low) | [plan](docs/plans/2026-09-11-1410-feat-jira-dependency-backend-plan.md) |

## v0.2.0 rebar review integration

Status. Planned.
Goal. The rebar review gates run on the native Bedrock path with per-leg failover and signed verdicts, and the review path's remaining rebar-dependent gaps are closed. Authored-identity signing depends on the object store introduced in v0.1.0.

| Feature | Effort | Detail |
|---|---|---|
| Native Bedrock review path | 6h | [plan](docs/plans/2026-09-02-2042-feat-native-bedrock-review-path-plan.md) |
| Signed review verdicts | 8h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Acceptance-gate live validation | 3-6h (medium) | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Authored-identity signing | 8-16h (low) | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |

Version effort. 25-36h.

## v0.3.0 AWS transition and FedRAMP readiness

Status. Planned.
Goal. The platform has documented local-to-AWS mappings, committed audit and evidence contracts, an S3-backed WORM evidence and audit store, a FedRAMP Medium hardening design, and the connectivity a deployed environment needs. The WORM store shares the object store stood up in v0.1.0.

| Feature | Effort | Detail |
|---|---|---|
| Local-to-AWS mapping document | 3h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Audit and evidence schema contracts | 4h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| S3-backed WORM evidence and audit store | 10h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| FedRAMP Medium hardening design | 8h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Evidence retention configuration | 2h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Private-endpoint jump-host container image | 6h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Full evidence artifact coverage | 6h | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |
| Remaining connectivity and sandbox confirmations | 4-9h (low) | [roadmap](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) |

Version effort. 43-48h.

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
