"""Platform review-tier resolver for the native Bedrock review path.

Each review tier resolves to a Bedrock Claude primary model. The Claude-to-Nova
fallback chain is configured in rebar's model-class config file, keyed by the
primary model id, so rebar builds the ``FallbackModel`` natively and prompt caching
survives the wrap. rebar exposes three fixed classes keyed by primary, so the two
tiers that share the Opus primary share one Nova fallback.
"""

from __future__ import annotations

BEDROCK_PROVIDER = "bedrock"

# The primary and fallback model ids per tier. The primary is what the native review
# config dispatches on. The fallback ids are the intended chain and must match the
# rebar model-class config in config/rebar-review.toml, which is what rebar actually
# reads at runtime; test_review_tiers asserts the two stay in agreement.
REVIEW_TIER_MODELS: dict[str, tuple[str, str]] = {
    "review-model": (
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.amazon.nova-pro-v1:0",
    ),
    "security-review-model": (
        "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "us.amazon.nova-pro-v1:0",
    ),
    "evaluation-model": (
        "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "us.amazon.nova-pro-v1:0",
    ),
}


class UnknownReviewTierError(ValueError):
    """A review tier has no configured native Bedrock model mapping."""


def resolve_review_tier(tier: str) -> tuple[str, str]:
    """Return the (primary, fallback) Bedrock model ids for a review tier."""
    try:
        return REVIEW_TIER_MODELS[tier]
    except KeyError:
        raise UnknownReviewTierError(
            f"no native Bedrock model mapping for review tier {tier!r}"
        ) from None


def review_primary_ref(tier: str) -> str:
    """Return the provider-qualified primary model reference for a review tier."""
    primary, _ = resolve_review_tier(tier)
    return f"{BEDROCK_PROVIDER}:{primary}"


def review_fallback_ref(tier: str) -> str:
    """Return the provider-qualified Nova fallback reference for a review tier."""
    _, fallback = resolve_review_tier(tier)
    return f"{BEDROCK_PROVIDER}:{fallback}"
