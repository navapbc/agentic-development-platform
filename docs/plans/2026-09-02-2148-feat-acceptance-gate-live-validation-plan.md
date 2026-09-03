---
title: Acceptance-Gate Live Validation - Plan
type: test
date: 2026-09-02
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Acceptance-Gate Live Validation - Plan

## Goal Capsule

Objective. An operator can trust that the acceptance gate scores completion criteria correctly, backed by a recorded live result, and the acceptance contract is adjusted when the evidence shows the reused code-review attributes score completion poorly.

Means. Run the acceptance gate live against the acceptance gold set through the existing review-eval harness, measure verdict and finding quality, and either confirm the reused contract or adjust it (KTD1, KTD2).

Product authority. Enriches the "Acceptance-gate live validation" feature in the roadmap at [remaining-work roadmap plan](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) and its v0.2.0 grouping in [ROADMAP.md](ROADMAP.md).

Stop conditions. The exit is a recorded live result, not a hermetic unit assertion, because it turns on model scoring behavior. No change to the multi-pass kernel structure.

## Product Contract

Product Contract preservation. Sourced from the "Acceptance-gate live validation" roadmap feature; requirements restate its subtasks with no scope change.

### Summary

The acceptance gate reuses the code-review finder and verification contracts. Run it live over the acceptance gold cases through the review-eval harness, measure whether the code-review verification attributes score unmet acceptance criteria and unsupported completion claims well, and record the result. If the scoring is inadequate, adjust the acceptance contract, its criteria, prompt, or a dedicated acceptance verify schema, until it holds.

### Problem Frame

The acceptance stage maps to `code_review_findings` and `code_review_verification` in the kernel, so it judges completion with attributes tuned for code defects. That was wired without live confirmation. Code-defect verification attributes may not score completion criteria as well as they score code, so the gate could pass unmet criteria or block sound completions. Only a live run against known-labeled cases can tell.

### Requirements

- R1. The acceptance gate runs live over the acceptance gold cases and produces a scorecard with verdict accuracy and finding precision, recall, and F1 for the acceptance stage.
- R2. The result is recorded as a committed scorecard under the eval scorecards directory, dated and attributable to the kernel and model tier that produced it.
- R3. The result includes a judgment on whether the code-review verification attributes score completion criteria adequately, against a stated threshold.
- R4. If the judgment is inadequate, the acceptance contract is adjusted, its criteria, prompt, or a dedicated acceptance verify schema, and re-run until it meets the threshold or a recorded decision documents why it stays as is.
- R5. The acceptance gold set covers the completion shapes that matter, at least met criteria, unmet criteria, and unsupported completion claims.

### Success Criteria

- The recorded scorecard shows the acceptance stage meets the adequacy threshold (KTD3), or a recorded decision documents the reasoned choice to keep the contract as is. This is the feature's success signal.
- The recorded scorecard lets a reader see acceptance-stage verdict accuracy and per-finding precision and recall without rerunning the harness.
- A later change to the acceptance contract can be measured against this baseline.

### Scope Boundaries

- No change to the plan, code, or security stages.
- No change to the multi-pass kernel structure; only the acceptance contract, criteria, prompt, or verify schema may change, and only if the evidence requires it.
- This is validation, not a new gate.

### Outstanding Questions

- Resolve before planning is not required; the threshold in R3 is set in U2 from the existing review-eval baseline conventions.
- Deferred to planning. Whether an inadequate result leads to a dedicated acceptance verify schema or a prompt and criteria change. Resolve in U3 from the measured failure mode.

### Sources

- Acceptance path: `services/review/kernel.py` (`ACCEPTANCE_VERIFICATION` in `_MULTIPASS`, reusing `code_review_findings`/`code_review_verification`), `services/orchestration/temporal_activities/review.py` (`verify_acceptance`, `_ACCEPTANCE`, `_run_gate`).
- Eval harness: `services/review/eval/harness.py` (`build_rebar_reviewer`, `score_stage_aware`, `StageScore`), gold set `services/review/eval/gold/review-gates.jsonl` (acceptance cases such as `accept-complete-evidence`, `accept-unmet-acceptance-criteria`, `accept-destructive-migration`), scorecards `services/review/eval/scorecards/`, `inv review-eval` task and `scripts/review_eval.py`.
- Acceptance criteria input: `services/review/types.py` (`ReviewInput` fields), the acceptance prompt in `services/prompts/` (`acceptance-verification`).
- rebar contract: `rebar/llm/code_review` (`code_review_verification` registered on import).

## Planning Contract

### Key Technical Decisions

- KTD1. Validate through the existing review-eval harness stage-aware path, not a new harness. Governs R1, R2. Rationale: `build_rebar_reviewer` and `score_stage_aware` already score a stage against gold cases and emit `StageScore`, and `inv review-eval` already writes scorecards.
- KTD2. Run the acceptance stage live against the acceptance gold cases, not offline fixtures, since the question is model scoring behavior. Governs R1, R3. Rationale: the offline fixture cannot reveal whether the code-defect attributes score completion.
- KTD3. Set the adequacy threshold from the existing baseline conventions in the review-eval scorecards and treat a miss as the trigger for a contract adjustment. Governs R3, R4. Rationale: the harness already computes verdict accuracy and precision, recall, and F1, so the threshold is expressed in those terms.

### High-Level Design

Extend the gold coverage if needed, run the acceptance stage live, record the scorecard, judge against the threshold, and adjust the contract only if the evidence requires it.

- Gold coverage. Confirm the acceptance gold cases cover met, unmet, and unsupported-claim shapes; add cases if a shape is missing.
- Live run. Drive the acceptance stage through `build_rebar_reviewer` and `score_stage_aware` on the live kernel, producing an acceptance `StageScore`.
- Record. Write a dated scorecard under the eval scorecards directory, attributed to the kernel and model tier.
- Judge and adjust. Compare the scorecard against the threshold. If inadequate, adjust the acceptance criteria, prompt, or introduce a dedicated acceptance verify schema, then re-run.

### Assumptions

- The review-eval harness stage-aware path runs the acceptance stage as configured, verified present.
- A live run has model access through the review path (native or proxied); this validation does not require the native Bedrock path.
- The acceptance gold cases are representative enough, or can be extended within this feature.

### Sequencing

U1 (gold coverage) comes first. U2 (live run and scorecard) depends on U1. U3 (judge and adjust) depends on U2.

## Implementation Units

### U1. Acceptance gold coverage

Goal. Ensure the acceptance gold set covers met criteria, unmet criteria, and unsupported completion claims.
Requirements. R5.
Files. `services/review/eval/gold/review-gates.jsonl` (add acceptance cases if a shape is missing).
Approach. Audit the acceptance cases against the three completion shapes. Add labeled cases for any missing shape, following the existing record format (`case_id`, `stage`, `subject`, `expected_verdict`, `expected_findings`).
Test scenarios. The gold set contains at least one met, one unmet, and one unsupported-claim acceptance case. Each acceptance case has a labeled expected verdict and expected findings. The harness loads the acceptance cases without error.
Verification. `tests/unit/test_review_eval_gold.py` (new or extend), `inv test`.

### U2. Live acceptance run and scorecard

Goal. Run the acceptance stage live and record a dated scorecard.
Requirements. R1, R2, R3.
Files. `scripts/review_eval.py` (support an acceptance-stage live run and scorecard output), `services/review/eval/scorecards/` (the recorded scorecard), `tasks.py` (reuse or extend `inv review-eval`).
Approach. Drive the acceptance stage through `build_rebar_reviewer` and `score_stage_aware` on the live kernel (KTD1, KTD2). The harness scores `expected_findings` only for the keyword baseline today, so add a normalization step that maps rebar acceptance findings to the canonical gold labels and emit finding precision, recall, and F1 for the rebar kernel alongside verdict accuracy. Write a dated scorecard with the acceptance `StageScore`, attributed to the kernel and model tier. Record the adequacy threshold (KTD3) in the scorecard so the judgment in U3 is traceable.
Test scenarios. A live acceptance run produces a `StageScore` with verdict accuracy and per-finding precision and recall for the rebar kernel, with rebar findings normalized to the canonical gold labels. The scorecard is written to the scorecards directory, dated, and names the kernel and model tier. The threshold is recorded in the scorecard. The offline fixture path still works for hermetic CI.
Verification. `tests/unit/test_review_eval_acceptance.py` (new, over a fake kernel for the harness plumbing), a live `inv review-eval` run recorded in the scorecard, `inv test`.

### U3. Judge and adjust the acceptance contract

Goal. Judge the scorecard against the threshold and adjust the acceptance contract if inadequate.
Requirements. R3, R4.
Files. `services/review/kernel.py` (acceptance `_MULTIPASS` entry, if the criteria or verify schema changes), the acceptance prompt in `services/prompts/`, `services/review/eval/scorecards/` (the re-run scorecard).
Approach. Compare the recorded acceptance scorecard against the threshold. If it meets the threshold, record the confirmation. If it misses, adjust the acceptance criteria, the acceptance prompt, or introduce a dedicated acceptance verify schema in place of the reused `code_review_verification`, then re-run U2 and record the improved scorecard. Resolve the adjustment-shape Outstanding Question from the measured failure mode.
Test scenarios. When the scorecard meets the threshold, a recorded confirmation names the score and threshold. When it misses, the adjusted contract re-runs and the new scorecard is recorded. The adjustment does not change the plan, code, or security stages. A recorded decision documents any choice to keep the contract as is with the measured reason.
Verification. The recorded before-and-after scorecards, `tests/unit/test_review_kernel.py` (extend if the acceptance schema changes), `inv ci`.

## Verification Contract

- Lint. `ruff check` clean. Types. `basedpyright` clean on `services/review` and `scripts/review_eval.py`.
- Hermetic. `inv ci` green, including the offline review-eval fixture path.
- Full suite. `inv test` green.
- Live exit. The recorded acceptance scorecard meets the stated threshold, or a recorded decision documents why the contract stays as is. This is the feature's real exit and is not a hermetic assertion.
- Regression floor. The plan, code, and security stages score unchanged against their gold cases.

## Definition of Done

- Global. R1 through R5 are met. `inv ci` and `inv test` are green. Abandoned code is removed from the diff.
- U1. The acceptance gold set covers met, unmet, and unsupported-claim shapes.
- U2. A live acceptance scorecard is recorded, dated, and attributed, with the threshold captured.
- U3. The scorecard meets the threshold, or the contract was adjusted and re-run, or a recorded decision documents why it stays as is, with no change to the other stages.
