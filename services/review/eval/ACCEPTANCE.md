# Acceptance-gate validation

This directory holds the labeled set, the scoring harness, and the recorded scorecards for validating the acceptance verification gate. The acceptance gate judges whether an implemented change meets a ticket's acceptance criteria. This validation measures whether the gate's verdicts agree with a known-correct outcome over a fixed set.

This validation is distinct from the deterministic review-gate quality harness in the same package. `harness.py` scores a keyword-marker reviewer for the hermetic CI eval gate. `acceptance_harness.py` runs the acceptance gate through the production kernel path and scores it against met and unmet labels, so it exercises the actual reviewer rather than a proxy.

## Labeled set

`acceptance_set.jsonl` is a JSONL file. An optional first `meta` record carries notes and a baseline tag. Every other record is a `case` with these fields.

- `case_id` is a stable identifier for the case.
- `acceptance_criteria` is what the ticket required.
- `completion_evidence` is what a run reported as its completion.
- `outcome` is the ground truth, either `met` or `unmet`.
- `notes` is optional free text.

The set must contain at least one `met` and one `unmet` case so the scorecard can measure both false accepts and false blocks. The current cases are authored seed data pending expansion from real run outputs during the batch validation.

## Scoring and the adequacy bar

The harness maps each verdict to a predicted outcome. A `pass` verdict predicts `met`. Any other verdict, `block` or `indeterminate`, predicts `unmet`, because a non-pass verdict does not accept the completion. Each prediction scores against the label as an agreement, a false accept, or a false block.

- Agreement is a prediction that matches the label.
- A false accept is a `met` prediction on an `unmet` case. The gate approved incomplete work.
- A false block is an `unmet` prediction on a `met` case. The gate rejected complete work.

The adequacy bar is fixed in `acceptance_harness.py` before any run and is read rather than computed from results. False accepts are the dangerous error, so the bar admits none. A limited false-block rate is tolerated. See `ADEQUACY_BAR` in `acceptance_harness.py` for the exact thresholds.

## Running

The live scored run requires the configured review path and consumes model calls. It is run during the batch end-to-end validation. The command is documented in `acceptance_harness.py` and writes a scorecard under `scorecards/`.
