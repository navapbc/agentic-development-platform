"""Evidence classification vocabulary.

A leaf module so both the settings layer and the evidence layer share one definition of
the valid evidence classifications without a circular import. Classification drives the
retention a record receives through the classification-to-retention mapping.
"""

from __future__ import annotations

CLASSIFICATION_STANDARD = "standard"
CLASSIFICATION_SECURITY = "security"
CLASSIFICATION_AUDIT = "audit"

EVIDENCE_CLASSIFICATIONS = frozenset(
    {CLASSIFICATION_STANDARD, CLASSIFICATION_SECURITY, CLASSIFICATION_AUDIT}
)


class EvidenceClassificationError(ValueError):
    """A classification value is outside the defined vocabulary."""


def validate_classification(value: str) -> str:
    """Return ``value`` when it is a defined classification, else raise."""
    if value not in EVIDENCE_CLASSIFICATIONS:
        raise EvidenceClassificationError(
            f"unknown evidence classification {value!r}. expected one of "
            f"{sorted(EVIDENCE_CLASSIFICATIONS)}"
        )
    return value
