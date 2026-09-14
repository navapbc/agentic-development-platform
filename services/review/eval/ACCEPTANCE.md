# Acceptance-gate validation

This directory holds the labeled set, the scoring harness, and the recorded scorecards for validating the acceptance verification gate. The acceptance gate judges whether an implemented change meets a ticket's acceptance criteria. This validation measures whether the gate's verdicts agree with a known-correct outcome over a fixed set.

This validation is distinct from the deterministic review-gate quality harness in the same package. `harness.py` scores a keyword-marker reviewer for the hermetic CI eval gate. `acceptance_harness.py` runs the acceptance gate through the production kernel path over a real change and scores it against met and unmet labels, so it exercises the actual reviewer.

## Grounded labeled set

The set lives under `acceptance_cases/`, one directory per case. Each case grounds the review in a real change so the gate reviews an actual diff, the way production reviews an implemented change at its commit. A case directory contains:

- `case.json` with `case_id`, `acceptance_criteria` (what the ticket required), `change_summary` (the completion a run would report), `outcome` (`met` or `unmet`), and an optional `notes`.
- `base/` a file tree at the starting state.
- `head/` the file tree after the change.

The harness materializes `base/` and `head/` into a temporary git repository, committing the base tree then the head tree, so the head commit carries the change as a real base-to-head diff. It builds the acceptance review input grounded at that commit and runs the gate. The set must contain at least one `met` and one `unmet` case, and each case must have a non-empty diff. The temporary repositories are removed after each run, so the committed inputs are the only persistent state.

## Adding cases from an implemented change

`snapshot_case` in `acceptance_schema.py` captures a repository's base-to-head diff into a new case directory. Given a repository, a base ref, a head ref, and the case labels, it writes each changed file's base and head content and a `case.json`, then validates the result loads. Use it to turn a workflow run's implemented change into a labeled case for the set.

## Scoring and the adequacy bar

The harness maps each verdict to a predicted outcome. A `pass` verdict predicts `met`. Any other verdict, `block` or `indeterminate`, predicts `unmet`, because a non-pass verdict does not accept the completion. Each prediction scores against the label as an agreement, a false accept, or a false block.

- Agreement is a prediction that matches the label.
- A false accept is a `met` prediction on an `unmet` case. The gate approved incomplete work.
- A false block is an `unmet` prediction on a `met` case. The gate rejected complete work.

The adequacy bar is fixed in `acceptance_harness.py` before any run and is read rather than computed from results. False accepts are the dangerous error, so the bar admits none. A limited false-block rate is tolerated. See `ADEQUACY_BAR` in `acceptance_harness.py` for the exact thresholds.

## Running

The live scored run requires the configured review path and consumes model calls. It is run during the batch end-to-end validation.

    python -m services.review.eval.acceptance_harness

It writes a scorecard under `scorecards/`. When the scorecard misses the bar, the acceptance stage needs its own finder and verify contract tuned to completion-criteria coverage, replacing the borrowed code-review contract for that stage, after which the run repeats until the scorecard clears the bar.
