---
title: Acceptance-Gate Live Validation - Plan
type: feat
date: 2026-09-11
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Acceptance-Gate Live Validation - Plan

## Goal Capsule

Objective. Confirm through a live run over a fixed ticket set that the acceptance verification gate scores completion criteria adequately, and adjust the acceptance contract until it does when the confirmation fails.

Means. Run the acceptance gate live over a fixed, labeled ticket set, score each verdict against a known-correct outcome for that ticket, judge whether the current contract, which reuses the code-review verification attributes, distinguishes met from unmet completion, and either record the confirmation or give the acceptance stage its own finder and verify contract and re-run until the scores are adequate.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "Acceptance-gate live validation" entry. It does not widen that feature's scope.

Stop conditions. The multi-pass kernel logic, the review activity boundary, and the workflow wiring are unchanged unless the assessment forces a contract adjustment. A contract change, if made, is confined to the acceptance stage and does not alter the plan, code, or security stages.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Acceptance-gate live validation". The requirements below restate that feature's subtasks as R-IDs with no scope change.

### Summary

The acceptance verification stage is wired but unvalidated. It runs the multi-pass review kernel over completion evidence, but it borrows the code-review finder and verify contracts, which score code correctness, regressions, test coverage, and plan adherence rather than whether the implemented change meets the ticket's stated acceptance criteria. This plan validates the gate empirically. It assembles a fixed ticket set with known acceptance outcomes, runs the gate live over that set, scores each verdict against the known outcome, and judges whether the current attributes separate a genuinely complete change from an incomplete one. When the judgment is inadequate, it gives the acceptance stage its own contract tuned to completion-criteria coverage and re-runs until the scores are adequate. The output is a recorded scorecard and either a confirmation that the current contract is adequate or a contract adjustment that makes it so.

### Problem Frame

`services/review/kernel.py` maps `ReviewStage.ACCEPTANCE_VERIFICATION` onto the `code_review_findings` and `code_review_verification` schemas, so the acceptance stage judges completion with attributes designed to find code defects. The `acceptance-verification/v1` prompt in `services/prompts/library.py` asks the model to judge whether the change satisfies the acceptance criteria and whether the evidence substantiates the completion claim, but the verify pass scores that judgment against code-review attributes. Whether those attributes score completion adequately is unconfirmed. The `verify_acceptance` activity records a verdict and the workflow surfaces it as `acceptance_verdict`, but no run has confirmed the verdict is trustworthy. This feature closes that gap with evidence rather than assertion.

### Requirements

- R1. A fixed, labeled ticket set is assembled where each ticket carries its acceptance criteria, the completion evidence a run produced, and a known-correct acceptance outcome of met or unmet.
- R2. The acceptance gate runs live over that set and records a verdict, findings, and provenance for each ticket.
- R3. Each verdict is scored against the ticket's known outcome, producing a scorecard of agreement, false accepts, and false blocks over the set.
- R4. The scorecard is assessed against a stated adequacy bar that separates a genuinely complete change from an incomplete one, and the assessment is recorded.
- R5. When the assessment is inadequate, the acceptance stage receives its own finder and verify contract tuned to completion-criteria coverage, replacing the borrowed code-review contract for that stage only, and the gate is re-run until the scorecard clears the bar.
- R6. The validation is reproducible from committed inputs and a documented command, so a later change to the acceptance path can re-run it.

### Scope Boundaries

- The plan, code, and security review stages are out of scope. Only the acceptance verification stage is validated and, if needed, adjusted.
- This plan does not change the multi-pass kernel logic, the review activity boundary, or the workflow wiring, except to register a new acceptance contract when the assessment forces one.
- Assembling the ticket set uses existing run outputs and rebar tickets. It does not build a new corpus-generation pipeline.
- The live run consumes model calls through the configured review path and does not change that path.

### Outstanding Questions

- Deferred to planning. Whether the fixed ticket set is drawn from the rebar store used in the v0.1.0 end-to-end validation or from a purpose-labeled set. Resolve in U1. Non-blocking, since either source yields labeled tickets.
- Deferred to planning. Whether an adequate assessment still warrants a dedicated acceptance contract for clarity even when the borrowed contract passes. Resolve in U4. Non-blocking.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Acceptance stage mapping: `services/review/kernel.py` (`_STAGE_SPECS[ReviewStage.ACCEPTANCE_VERIFICATION]`, `_register_and_impact`).
- Acceptance activity and prompt: `services/orchestration/temporal_activities/review.py` (`_ACCEPTANCE`, `verify_acceptance`, `_run_gate`), `services/prompts/library.py` (`ACCEPTANCE_VERIFICATION`).
- Workflow surfacing: `services/orchestration/temporal_workflows/draft_pull_request.py` (`_ACCEPTANCE`, `acceptance_verdict`).
- Evaluation harness precedent: `services/repository_intelligence/eval/` (gold set, harness, scorecards) as the pattern for a committed, reproducible scored run.
- rebar review contracts (installed `nava-rebar` 0.12.0): `rebar.llm.code_review` (registers the code contracts), `rebar.llm.review_kernel` (`impact_code`), and the finder and verify schema registration the kernel calls.

## Planning Contract

### Key Technical Decisions

- KTD1. Validate with a labeled scored run, not an assertion. Assemble a fixed ticket set with known met and unmet outcomes and score the gate's verdicts against them. Governs R1, R2, R3. Rationale. The feature's whole purpose is to replace an unconfirmed wiring with evidence, so the deliverable is a scorecard, not a claim.
- KTD2. State the adequacy bar before running. Define what agreement, false-accept, and false-block rates count as adequate, so the assessment is a measurement against a fixed bar rather than a judgment after the fact. Governs R4. Rationale. A bar set after seeing results is not a test, so the bar precedes the run.
- KTD3. Adjust the contract only for the acceptance stage and only on failure. When the scorecard misses the bar, register a dedicated acceptance finder and verify schema tuned to completion-criteria coverage, leaving the other stages on their contracts. Governs R5. Rationale. The borrowed code-review contract is the suspected cause, so the narrowest fix replaces it for this stage alone.
- KTD4. Make the run reproducible from committed inputs. Commit the ticket set, the labels, and a documented command, following the repository-intelligence eval harness pattern. Governs R6. Rationale. A one-off run cannot guard a later regression, so the validation has to re-run on demand.

### High-Level Design

A validation harness reads a committed ticket set, each entry carrying acceptance criteria, completion evidence, and a known outcome. The harness runs the acceptance gate over each entry through the same kernel path production uses, collects the verdict and findings, and scores each against the known outcome into a scorecard of agreement, false accepts, and false blocks. The harness compares the scorecard to a stated adequacy bar and writes an assessment. When the assessment is inadequate, a dedicated acceptance finder and verify contract is registered for the acceptance stage, tuned to score completion-criteria coverage rather than code defects, and the kernel maps the acceptance stage onto it. The harness re-runs until the scorecard clears the bar. The committed ticket set, the command, and the latest scorecard live under the evaluation tree so the validation reproduces.

### Assumptions

- Enough labeled tickets exist from prior runs or the rebar store to form a set that contains both met and unmet completions, so the scorecard can measure false accepts and false blocks.
- The configured review path is reachable for the live run, the same path the review gates use in the end-to-end validation. This plan validates whichever review path is configured, so it runs after the [Native Bedrock review path](2026-09-02-2042-feat-native-bedrock-review-path-plan.md) lands when that path is the intended target, and the scorecard records which path produced it.
- The kernel's stage-to-schema mapping is the single point that binds the acceptance stage to a contract, so a dedicated contract is a localized change.

### Sequencing

U1 assembles and commits the labeled ticket set. U2 builds the scoring harness and states the adequacy bar. U3 runs the gate live and records the scorecard. U4 assesses and, on failure, adds a dedicated acceptance contract and re-runs. U2 depends on U1. U3 depends on U2. U4 depends on U3.

## Implementation Units

### U1. Labeled acceptance ticket set

Goal. Assemble and commit a fixed ticket set where each entry carries acceptance criteria, completion evidence, and a known met or unmet outcome.
Requirements. R1, R6.
Files. `services/review/eval/acceptance_set.jsonl` (new, the labeled entries) or a directory under `services/review/eval/`, a short `README` describing the labels.
Approach. Draw tickets from prior run outputs and the rebar store, selecting a set that contains both met and unmet completions. For each, record the acceptance criteria, the completion evidence a run produced, and the known-correct outcome. Keep the set small enough to run in minutes and varied enough to expose a contract that cannot tell met from unmet.
Test scenarios. The set loads as structured entries. Every entry carries criteria, evidence, and a met or unmet label. The set contains at least one met and one unmet entry.
Verification. `tests/unit/test_acceptance_eval_set.py` (new, structural), `inv test`.

### U2. Scoring harness and adequacy bar

Goal. Build a harness that runs the acceptance gate over the set and scores each verdict against the known outcome, and state the adequacy bar before any run.
Requirements. R3, R4.
Files. `services/review/eval/acceptance_harness.py` (new), a documented adequacy bar in the harness or its README.
Approach. For each entry, build the `ReviewInput` the acceptance stage expects and run the gate through the production kernel path. Score the verdict against the label into agreement, false accept, and false block counts. State the adequacy bar as concrete thresholds on those counts before running, so the assessment measures against a fixed bar. Follow the repository-intelligence eval harness pattern for structure and output.
Test scenarios. The harness scores a matching verdict as agreement, a block on a met ticket as a false block, and an accept on an unmet ticket as a false accept. The adequacy bar is a fixed, documented threshold the harness reads rather than computes from results.
Verification. `tests/unit/test_acceptance_harness.py` (new, over the scoring logic with a fake kernel), `inv test`.

### U3. Live scored run

Goal. Run the acceptance gate live over the set and record the scorecard.
Requirements. R2, R3, R6.
Files. `services/review/eval/scorecards/acceptance.<date>.json` (new, the recorded scorecard), a documented `inv` or module command to run it.
Approach. Run the harness against the configured review path over the committed set, collecting each verdict, findings, and provenance. Write the scorecard with per-ticket outcomes and the aggregate agreement, false-accept, and false-block rates. Document the command so the run reproduces.
Test scenarios. The command produces a scorecard over the full set. The scorecard carries per-ticket outcomes and aggregate rates. Re-running over the same set and path produces a comparable scorecard.
Verification. The recorded scorecard file, a documented command, `inv test` for the harness unit contract.

### U4. Assessment and contingent contract adjustment

Goal. Assess the scorecard against the bar, record the assessment, and when inadequate give the acceptance stage its own contract and re-run until the scorecard clears the bar.
Requirements. R4, R5.
Files. `services/review/eval/` (the recorded assessment), and on a failing assessment `services/review/kernel.py` (map the acceptance stage to a dedicated contract) plus the acceptance finder and verify schema registration.
Approach. Compare the scorecard to the stated bar and record whether the current contract is adequate. When it is not, register a dedicated acceptance finder and verify schema tuned to completion-criteria coverage, map `ReviewStage.ACCEPTANCE_VERIFICATION` onto it in the kernel, leaving the other stages unchanged, and re-run U3 until the scorecard clears the bar. When the current contract is adequate, record the confirmation and make no code change.
Test scenarios. An adequate scorecard records a confirmation and changes no stage mapping. An inadequate scorecard drives a dedicated acceptance contract, and after the change the acceptance stage maps to it while the plan, code, and security stages are unchanged. A re-run after the change clears the bar.
Verification. `tests/unit/test_review_kernel.py` (extend only if a mapping changes), the updated scorecard, `inv ci`.

## Verification Contract

- Lint. `ruff check` clean.
- Types. `basedpyright` clean on any changed `services/review` source, with no new baseline entries in the changed files.
- Unit and end-to-end. `inv ci` green.
- Full suite. `inv test` green.
- Evidence floor. A committed scorecard and a recorded assessment exist, and the run reproduces from the committed set and the documented command.
- Regression floor. When no contract change is made, every pre-existing review test passes with no assertion change. When a dedicated acceptance contract is added, the plan, code, and security stage mappings and their tests are unchanged.

## Definition of Done

- Global. R1 through R6 are met. `inv ci` and `inv test` are green. Abandoned or experimental code is removed from the diff.
- U1. A committed labeled ticket set carries criteria, evidence, and met or unmet outcomes, with at least one of each.
- U2. The harness scores verdicts against labels, and the adequacy bar is fixed and documented before the run.
- U3. A live scored run produces a committed scorecard with per-ticket and aggregate outcomes, reproducible from a documented command.
- U4. The assessment against the bar is recorded, and either the current contract is confirmed adequate with no code change, or a dedicated acceptance contract is added for the acceptance stage alone and a re-run clears the bar.
