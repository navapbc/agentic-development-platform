"""Resolve a user-requested gate model to an available model, across providers.

Gates and other user-defined items name whatever model a user wants, which the platform cannot vet
ahead of time. This resolver maps that request to a model the account actually has, staying inside
the same vendor and family so a request never silently crosses to a different capability tier. An
exact match runs unchanged. A within-family version substitution either warns and proceeds or
aborts, controlled by the caller. A request with no same-family model available always aborts.

Parsing is provider-agnostic. A model id is split into tokens. Pure-numeric tokens form the
version read as semver, an eight-digit token is a release date, and every token containing a
letter is part of the family, which carries the line name and the tier words such as ``opus``,
``pro``, ``flash``, ``mini``, ``4o`` or ``70b``. Because tier words live in the family and the
first version number is the major revision, nearest-match never crosses a tier and never crosses a
major revision. The result is that anthropic, openai, google, meta, mistral, amazon and others
resolve through one path.

Platform-internal roles do not use this resolver. They are validated before a release and rely on
their explicit primary and fallback instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WARN = "warn"
ABORT = "abort"

_REGION_PREFIXES = ("us.", "eu.", "apac.", "us-gov.")
_VERSION_SUFFIX = re.compile(r"-v\d+(:\d+)?$")
_DATE_TOKEN = re.compile(r"^\d{8}$")
_NUMERIC_TOKEN = re.compile(r"^\d+$")

# Line name to vendor, used only when a request carries no explicit vendor namespace. The line
# name also lives in the family, so this map is a convenience for grouping, not a gate. Extend it
# as providers are added.
_VENDOR_BY_LINE = {
    "claude": "anthropic",
    "gpt": "openai",
    "chatgpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "codex": "openai",
    "gemini": "google",
    "llama": "meta",
    "mistral": "mistral",
    "mixtral": "mistral",
    "codestral": "mistral",
    "nova": "amazon",
    "titan": "amazon",
    "command": "cohere",
    "jamba": "ai21",
    "deepseek": "deepseek",
    "copilot": "github",
    "grok": "xai",
    "qwen": "alibaba",
}


class ModelUnavailableError(Exception):
    """A requested gate model has no acceptable match in the available catalog."""


@dataclass(frozen=True)
class ModelRef:
    """A parsed model reference keyed for family-scoped nearest-match."""

    vendor: str
    family: str
    version: tuple[int, ...]
    date: str
    raw: str

    @property
    def version_rank(self) -> int:
        # Rank as a decimal reading of the version, major*100 + minor*10 + patch, so 4.9 sits one
        # step below 5.0 and nearest-match reflects how a person reads a version number. Components
        # are assumed to be single digit, which holds for current provider model versions.
        components = list(self.version[:3]) + [0, 0, 0]
        return components[0] * 100 + components[1] * 10 + components[2]


@dataclass(frozen=True)
class ModelResolution:
    """The outcome of resolving a request against the catalog."""

    model_id: str
    requested: str
    substituted: bool
    note: str = ""


@dataclass(frozen=True)
class CatalogEntry:
    """One available model, its serving id paired with its parsed reference."""

    model_id: str
    ref: ModelRef


def _strip_prefixes(text: str) -> str:
    body = text.strip()
    if body.startswith("bedrock/"):
        body = body[len("bedrock/") :]
    for region in _REGION_PREFIXES:
        if body.startswith(region):
            body = body[len(region) :]
            break
    return body


def _normalize(text: str) -> str:
    body = text.strip().lower().replace(" ", "-")
    # Treat a dotted version such as gpt-4.1 the same as its hyphen form so tokenizing is uniform.
    body = re.sub(r"(?<=\d)\.(?=\d)", "-", body)
    return body


def parse_model_ref(text: str) -> ModelRef | None:
    """Parse a serving id or a user request into a vendor, family, and version reference.

    Returns ``None`` only when nothing usable remains, so a family with no version still parses and
    can take part in matching.
    """
    if not text or not text.strip():
        return None
    body = _strip_prefixes(_normalize(text))

    vendor = ""
    if "." in body:
        vendor, body = body.split(".", 1)
    body = _VERSION_SUFFIX.sub("", body)

    family_tokens: list[str] = []
    version: list[int] = []
    date = ""
    for token in (t for t in body.split("-") if t):
        if _DATE_TOKEN.match(token):
            date = token
        elif _NUMERIC_TOKEN.match(token):
            version.append(int(token))
        else:
            family_tokens.append(token)

    if not family_tokens:
        return None
    if not vendor:
        vendor = _VENDOR_BY_LINE.get(family_tokens[0], "")

    return ModelRef(
        vendor=vendor,
        family="-".join(family_tokens),
        version=tuple(version),
        date=date,
        raw=text.strip(),
    )


def build_catalog(model_ids: list[str]) -> list[CatalogEntry]:
    """Build a catalog from serving ids, dropping any that do not parse into a family."""
    entries: list[CatalogEntry] = []
    for model_id in model_ids:
        ref = parse_model_ref(model_id)
        if ref is not None:
            entries.append(CatalogEntry(model_id=model_id, ref=ref))
    return entries


def available_models_from_litellm(litellm_config: dict) -> list[str]:
    """Extract the distinct Bedrock model ids a LiteLLM config can serve.

    Reads ``model_list[].litellm_params.model``, keeping first-seen order and dropping duplicates so
    the same Bedrock model referenced by several logical groups appears once in the catalog.
    """
    seen: set[str] = set()
    models: list[str] = []
    for entry in litellm_config.get("model_list", []) or []:
        if not isinstance(entry, dict):
            continue
        params = entry.get("litellm_params") or {}
        model = str(params.get("model", "")).strip()
        if model and model not in seen:
            seen.add(model)
            models.append(model)
    return models


def available_gate_model_groups(litellm_config: dict) -> list[str]:
    """Extract the LiteLLM group names that name a client-recognized provider model.

    A gate's model call must send a name that both routes through the gateway and is recognized by
    the AI CLI. Those are the groups whose ``model_name`` parses to a known vendor line, for example
    ``claude-opus-4-5-20251101``. Semantic-alias groups such as ``security-review-model`` do not
    name a provider model, so they are excluded from the gate catalog. First-seen order is kept.
    """
    seen: set[str] = set()
    groups: list[str] = []
    for entry in litellm_config.get("model_list", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("model_name", "")).strip()
        if not name or name in seen:
            continue
        ref = parse_model_ref(name)
        if ref is not None and ref.vendor:
            seen.add(name)
            groups.append(name)
    return groups


def resolve_from_available(
    requested: str,
    available_model_ids: list[str],
    *,
    on_unavailable: str = ABORT,
) -> ModelResolution:
    """Resolve ``requested`` against a flat list of available serving ids."""
    return resolve_model(
        requested, build_catalog(available_model_ids), on_unavailable=on_unavailable
    )


def _serving_id(model_id: str) -> str:
    body = model_id.strip()
    return body[len("bedrock/") :] if body.startswith("bedrock/") else body


def _vendors_compatible(a: str, b: str) -> bool:
    return a == b or not a or not b


def resolve_model(
    requested: str,
    catalog: list[CatalogEntry],
    *,
    on_unavailable: str = ABORT,
) -> ModelResolution:
    """Resolve ``requested`` to an available serving id, applying nearest-match within a family.

    An exact serving-id match resolves with no substitution. A request whose family and full
    version match an available model, differing only by release date, is also not a substitution,
    since it names the same model version. Otherwise the nearest available model in the same
    vendor, family, and major revision is chosen by absolute version distance, breaking ties toward
    the newer version and then the newer release date. The fallback never crosses a major revision.
    When no model shares the requested family and major revision, or when a substitution is required
    and ``on_unavailable`` is ``abort``, a ``ModelUnavailableError`` is raised.
    """
    requested_serving = _serving_id(requested)
    for entry in catalog:
        if _serving_id(entry.model_id) == requested_serving:
            return ModelResolution(model_id=entry.model_id, requested=requested, substituted=False)

    want = parse_model_ref(requested)
    if want is None:
        raise ModelUnavailableError(
            f"requested gate model {requested!r} is not a recognized model reference"
        )

    family_label = f"{want.vendor}/{want.family}" if want.vendor else want.family
    want_major = want.version[0] if want.version else None

    candidates = [
        entry
        for entry in catalog
        if entry.ref.family == want.family
        and _vendors_compatible(entry.ref.vendor, want.vendor)
        and _same_major(entry.ref, want_major)
    ]
    if not candidates:
        if want_major is not None:
            raise ModelUnavailableError(
                f"no available model in major version {want_major} of family {family_label} "
                f"for request {requested!r}"
            )
        raise ModelUnavailableError(
            f"no available model in family {family_label} for request {requested!r}"
        )

    # A candidate with the same full version is the same model, not a substitution. Prefer the
    # newest release date among same-version candidates.
    if want.version:
        same_version = [e for e in candidates if e.ref.version == want.version]
        if same_version:
            best = min(same_version, key=lambda e: _date_key(e.ref.date))
            return ModelResolution(model_id=best.model_id, requested=requested, substituted=False)

    best = min(
        candidates,
        key=lambda e: (
            abs(e.ref.version_rank - want.version_rank),
            -e.ref.version_rank,
            _date_key(e.ref.date),
        ),
    )
    note = (
        f"requested {requested!r} is unavailable, substituting nearest in family "
        f"{family_label}: {best.model_id}"
    )
    if on_unavailable == ABORT:
        raise ModelUnavailableError(note + " (on_unavailable=abort)")
    return ModelResolution(model_id=best.model_id, requested=requested, substituted=True, note=note)


def _same_major(ref: ModelRef, want_major: int | None) -> bool:
    if want_major is None:
        return True
    return bool(ref.version) and ref.version[0] == want_major


def _date_key(date: str) -> int:
    return -int(date) if date.isdigit() else 0
