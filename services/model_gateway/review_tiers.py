"""Platform review-tier resolver for the native Bedrock review path.

Each review tier resolves to a Bedrock Claude model with no cross-family fallback.
A tier that cannot reach Claude fails once rebar exhausts its own retry budget rather
than answering from a weaker model, so a verdict always carries the quality the tier
was chosen for.
"""

from __future__ import annotations

BEDROCK_PROVIDER = "bedrock"

# The Bedrock model id per tier. Each must match the primary of the matching rebar class in
# config/local/rebar-review.toml, which is what rebar reads at runtime; test_review_tiers
# asserts the two stay in agreement.
REVIEW_TIER_MODELS: dict[str, str] = {
    "review-model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "security-review-model": "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "evaluation-model": "us.anthropic.claude-opus-4-5-20251101-v1:0",
}


class UnknownReviewTierError(ValueError):
    """A review tier has no configured native Bedrock model mapping."""


def resolve_review_tier(tier: str) -> str:
    """Return the Bedrock model id for a review tier."""
    try:
        return REVIEW_TIER_MODELS[tier]
    except KeyError:
        raise UnknownReviewTierError(
            f"no native Bedrock model mapping for review tier {tier!r}"
        ) from None


def review_primary_ref(tier: str) -> str:
    """Return the provider-qualified model reference for a review tier."""
    return f"{BEDROCK_PROVIDER}:{resolve_review_tier(tier)}"
