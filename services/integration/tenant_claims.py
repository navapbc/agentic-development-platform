"""The claims of a tenant's config, the values only one tenant may hold, and their clashes.

A claim is a kind and a canonical value. A tenant on the shared Slack app claims its channel as
slack-channel. A tenant on its own Slack app claims the app ID as slack-app and its channel as
slack-own-channel, which other tenants on their own apps may claim too and a tenant on the shared
app may not. A tenant on the Jira ticket backend claims each Jira project it reads as
jira-project, with the project's site and intake label in the value, so two tenants share a
project only under different intake labels. A tenant whose registries.yaml names a credential
claims its username as registry-username, so no two tenants present one registry identity.

A Slack value is the uppercase ID. A Jira value is https:// and the lowercase IDNA host of the
site, then the uppercase project key and the project's intake label, each after a slash. In the
tenant config bucket, a claim is the empty object claims/<kind>/<digest>/<tenant>, and the
marker with the earliest LastModified holds the value. Markers with one LastModified leave the
value to neither tenant. Each tenant's publisher keeps the index claims/_owners/<tenant>.json of
the claims it holds, and tags every bundle file it uploads with claims-protocol=1.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import yaml
from scripts.aws_clients import S3_CONFLICT_CODES, S3_MISSING_CODES, error_code

from services.integration.credentials import ConfigError
from services.integration.jira.credentials import JiraCredentialResolver, parse_jira_config
from services.integration.tickets.backend import JIRA
from services.orchestration.escalation import (
    CHANNEL_NAMES,
    ESCALATION_CONFIG,
    OWN_APP,
    SHARED_APP,
    SLACK,
)

SLACK_CHANNEL = "slack-channel"
SLACK_OWN_CHANNEL = "slack-own-channel"
SLACK_APP = "slack-app"
JIRA_PROJECT = "jira-project"
REGISTRY_USERNAME = "registry-username"
KINDS = (SLACK_CHANNEL, SLACK_OWN_CHANNEL, SLACK_APP, JIRA_PROJECT, REGISTRY_USERNAME)
CLAIMS_PREFIX = "claims"
JIRA_CONFIG = "jira"
REGISTRIES_CONFIG = "registries"
SLACK_ID = re.compile(r"^[A-Z0-9]+$")
OWNERS_PREFIX = f"{CLAIMS_PREFIX}/_owners/"
CLAIMS_PROTOCOL_TAG = "claims-protocol=1"
CLAIMS_PROTOCOL_KEY = "platform/claims-protocol.json"
CLAIMS_PROTOCOL_ABSENT = (
    f"{CLAIMS_PROTOCOL_KEY} is absent from the config bucket or not readable by this role. "
    "Terraform writes it once claims_backfilled is set, after every published value is claimed, "
    "and a tenant on the shared Slack app is refused until it exists"
)
CLAIM_FILES = (ESCALATION_CONFIG, JIRA_CONFIG, REGISTRIES_CONFIG)
INDEX_READERS = 8
_COUNTERPARTS = {SLACK_CHANNEL: SLACK_OWN_CHANNEL, SLACK_OWN_CHANNEL: SLACK_CHANNEL}


class ClaimRefusedError(ConfigError):
    """Another tenant holds a value the bundle claims, or no tenant does."""


def claims_protocol_in_force(client: Any, bucket: str) -> bool:
    """Return whether the config bucket holds the claims-protocol object.

    S3 answers AccessDenied for an absent key to a role that may not list it, so AccessDenied
    reads as absent. Any other error propagates, since it says nothing about the object.
    """
    try:
        client.get_object(Bucket=bucket, Key=CLAIMS_PROTOCOL_KEY)["Body"].read()
    except Exception as exc:
        if error_code(exc) in S3_MISSING_CODES | {"AccessDenied"}:
            return False
        raise
    return True


def names_shared_app(claims: Iterable[Claim]) -> bool:
    """Return whether the claims are those of a bundle on the shared Slack app."""
    return any(claim.kind == SLACK_CHANNEL for claim in claims)


@dataclass(frozen=True, order=True)
class Claim:
    kind: str
    value: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(f"{self.kind}\0{self.value}".encode()).hexdigest()

    @property
    def prefix(self) -> str:
        return f"{CLAIMS_PREFIX}/{self.kind}/{self.digest}/"

    def __str__(self) -> str:
        return f"{self.kind} {self.value}"


@dataclass(frozen=True)
class Clash:
    tenants: tuple[str, str]
    claims: tuple[Claim, Claim]

    def __str__(self) -> str:
        (first, second), (one, other) = self.tenants, self.claims
        if one == other:
            return f"tenants {first} and {second} both claim {one}"
        return f"tenant {first} claims {one} and tenant {second} claims {other}"


@dataclass(frozen=True)
class Marker:
    tenant: str
    last_modified: datetime


class _NoSecrets:
    def resolve(self, ref: str) -> str:
        return ""


def effective_config(files: Mapping[str, str], name: str) -> Mapping[str, Any]:
    """Return the parsed bundle file of the name without .yaml, empty when the bundle has none."""
    text = files.get(name)
    return (yaml.safe_load(text) or {}) if text is not None else {}


def _slack_id(field: str, value: object) -> str:
    canonical = str(value or "").strip().upper()
    if not SLACK_ID.match(canonical):
        raise ConfigError(f"slack.{field} {value} is not a Slack ID of letters and digits")
    return canonical


def _slack_claims(raw: Mapping[str, Any]) -> list[Claim]:
    slack = raw.get(SLACK)
    if SLACK not in raw.get("channels", CHANNEL_NAMES) or not isinstance(slack, Mapping):
        return []
    app = str(slack.get("app") or OWN_APP).strip()
    channel = slack_channel_claim(app, _slack_id("channel", slack.get("channel")))
    if app == SHARED_APP:
        return [channel]
    return [Claim(SLACK_APP, _slack_id("app_id", slack.get("app_id"))), channel]


def slack_channel_claim(app: str, channel: str) -> Claim:
    """Return the claim a tenant on the app makes on its Slack channel."""
    return Claim(SLACK_CHANNEL if app == SHARED_APP else SLACK_OWN_CHANNEL, channel.strip().upper())


def _jira_site(url: str) -> str:
    host = urlsplit(url.strip()).hostname or ""
    return f"https://{host.encode('idna').decode('ascii')}"


def _jira_value(url: str, project: str, label: str) -> str:
    return f"{_jira_site(url)}/{project.strip().upper()}/{label}"


def _jira_claims(raw: Mapping[str, Any]) -> list[Claim]:
    if not raw:
        return []
    config = parse_jira_config(raw)
    resolver = JiraCredentialResolver(config, set(config.allowlist), _NoSecrets())
    credentials = filter(None, map(resolver.resolve, sorted(resolver.projects())))
    return [
        Claim(
            JIRA_PROJECT,
            _jira_value(credential.url, credential.project, credential.intake_label),
        )
        for credential in credentials
    ]


def _registry_username(value: object) -> str:
    """Return the username lowercased, because registries such as Artifactory match it so."""
    username = str(value or "").strip()
    if not username or "/" in username or any(character.isspace() for character in username):
        raise ConfigError(f"registries.yaml credential.username {value!r} is not a username")
    return username.lower()


def _registry_claims(raw: Mapping[str, Any]) -> list[Claim]:
    credential = raw.get("credential") if isinstance(raw, Mapping) else None
    if not isinstance(credential, Mapping) or not credential.get("username"):
        return []
    return [Claim(REGISTRY_USERNAME, _registry_username(credential["username"]))]


def bundle_claims(files: Mapping[str, str], ticket_backend: str) -> frozenset[Claim]:
    """Return the claims of the bundle's files, keyed by name without .yaml.

    A tenant claims Jira projects only on the Jira ticket backend, which an empty backend selects.
    """
    claims = set(_slack_claims(effective_config(files, ESCALATION_CONFIG)))
    claims.update(_registry_claims(effective_config(files, REGISTRIES_CONFIG)))
    if (ticket_backend or JIRA).strip().lower() == JIRA:
        claims.update(_jira_claims(effective_config(files, JIRA_CONFIG)))
    return frozenset(claims)


def canonical_claim(kind: str, value: str) -> Claim:
    """Return the claim of a value written by hand, such as a lowercase channel ID.

    A Jira value is the site, the project key, and the intake label, each after a slash.
    """
    if kind not in KINDS:
        raise ConfigError(f"{kind} is not a claim kind. The kinds are {', '.join(KINDS)}")
    if kind == REGISTRY_USERNAME:
        return Claim(kind, _registry_username(value))
    if kind != JIRA_PROJECT:
        return Claim(kind, _slack_id("app_id" if kind == SLACK_APP else "channel", value))
    parts = value.strip().rsplit("/", 2)
    if len(parts) != 3:
        raise ConfigError(f"{value} is not a Jira site, project key, and intake label")
    return Claim(kind, _jira_value(*parts))


def kinds_contend(kind: str, other: str) -> bool:
    """Two claims on one value contend, except when both are channels of tenants on own apps."""
    return not kind == other == SLACK_OWN_CHANNEL


def _contested(claim: Claim) -> tuple[str, str]:
    kind = SLACK_CHANNEL if claim.kind == SLACK_OWN_CHANNEL else claim.kind
    return kind, claim.value


def find_clashes(claims: Mapping[str, Iterable[Claim]]) -> list[Clash]:
    """Return each pair of tenants that claim one value only one of them may hold.

    Tenants on their own Slack apps may claim one channel, and a tenant on the shared app may not
    claim the channel of a tenant on its own app.
    """
    holders: dict[tuple[str, str], list[tuple[str, Claim]]] = {}
    for tenant in sorted(claims):
        for claim in sorted(claims[tenant]):
            holders.setdefault(_contested(claim), []).append((tenant, claim))
    return [
        Clash((first, second), (one, other))
        for group in holders.values()
        for (first, one), (second, other) in itertools.combinations(group, 2)
        if first != second and kinds_contend(one.kind, other.kind)
    ]


def _parse_marker(key: str) -> tuple[str, str, str] | None:
    segments = key.split("/")
    if len(segments) != 4 or not all(segments):
        return None
    prefix, kind, digest, tenant = segments
    if prefix != CLAIMS_PREFIX or kind not in KINDS:
        return None
    return kind, digest, tenant


def _marker_tenant(key: str, claim: Claim) -> str | None:
    parsed = _parse_marker(key)
    if parsed is None or parsed[:2] != (claim.kind, claim.digest):
        return None
    return parsed[2]


def claim_markers(client: Any, bucket: str, kind: str, value: str) -> list[Marker]:
    """Return the markers on one claim, earliest first, listed from its one digest prefix.

    A key under the prefix other than claims/<kind>/<digest>/<tenant> is ignored.
    """
    claim = Claim(kind, value)
    markers = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=claim.prefix):
        for entry in page.get("Contents") or []:
            tenant = _marker_tenant(entry["Key"], claim)
            if tenant is not None:
                markers.append(Marker(tenant, entry["LastModified"]))
    return sorted(markers, key=lambda marker: (marker.last_modified, marker.tenant))


@dataclass(frozen=True)
class ClaimCheck:
    """The tenants whose markers contend for a value, as one tenant's check of its claim found them.

    The holders are the tenants with the earliest marker, and the rivals are the other tenants
    whose markers contend for the value.
    """

    tenant: str
    claim: Claim
    holders: tuple[str, ...]
    rivals: tuple[str, ...]

    @property
    def held(self) -> bool:
        return self.holders == (self.tenant,)

    @property
    def contested(self) -> bool:
        """Return whether another tenant's marker is the earliest or ties with the earliest."""
        return any(holder != self.tenant for holder in self.holders)

    def __str__(self) -> str:
        others = " and ".join(holder for holder in self.holders if holder != self.tenant)
        if self.tenant in self.holders:
            return (
                f"tenants {self.tenant} and {others} claimed {self.claim} at the same moment, "
                "so neither holds it"
            )
        if others and self.claim.kind == SLACK_CHANNEL:
            return (
                f"tenant {others} holds an earlier claim on {self.claim}, which a tenant on the "
                "shared app keeps for its button lifetime after it moves to another channel"
            )
        if others:
            return f"tenant {others} holds an earlier claim on {self.claim}"
        return f"tenant {self.tenant} has no marker on {self.claim}"


def claim_check(tenant: str, claim: Claim, markers: Mapping[str, Sequence[Marker]]) -> ClaimCheck:
    """Return which tenants hold the claim, from the markers of each kind listed on its value.

    The contenders are the tenant's own markers of the claim's kind and every other tenant's
    marker of a kind that contends with it, whether or not that tenant still names the value.
    """
    own = [marker for marker in markers.get(claim.kind, ()) if marker.tenant == tenant]
    rivals = [
        marker
        for kind, listed in markers.items()
        if kinds_contend(claim.kind, kind)
        for marker in listed
        if marker.tenant != tenant
    ]
    contenders = own + rivals
    earliest = min((marker.last_modified for marker in contenders), default=None)
    holders = {marker.tenant for marker in contenders if marker.last_modified == earliest}
    return ClaimCheck(
        tenant,
        claim,
        tuple(sorted(holders)),
        tuple(sorted({marker.tenant for marker in rivals})),
    )


def contending_kinds(kind: str) -> tuple[str, ...]:
    """Return the claim kinds whose markers on one value contend with a claim of the kind."""
    return (kind, _COUNTERPARTS[kind]) if kind in _COUNTERPARTS else (kind,)


@dataclass(frozen=True)
class ClaimIndex:
    """The claims a tenant's publisher holds, and the shared app channels it retains.

    A retained channel is one the tenant's bundle no longer names. Its marker stays until the
    release time, so the buttons already posted in it keep deciding the tenant's parks.
    """

    claims: frozenset[Claim] = frozenset()
    retained: Mapping[Claim, datetime] = field(default_factory=dict)

    def leaving(self, claims: Iterable[Claim]) -> frozenset[Claim]:
        """Return the shared app channels the index holds and the claims no longer name."""
        return frozenset(
            claim for claim in self.claims - frozenset(claims) if claim.kind == SLACK_CHANNEL
        )


def _index_body(index: ClaimIndex) -> bytes:
    body: dict[str, Any] = {
        "claims": [{"kind": claim.kind, "value": claim.value} for claim in sorted(index.claims)]
    }
    if index.retained:
        body["retained"] = [
            {"kind": claim.kind, "value": claim.value, "release_after": at.isoformat()}
            for claim, at in sorted(index.retained.items())
        ]
    return json.dumps(body, indent=2).encode()


def _parse_index(body: bytes) -> ClaimIndex:
    raw = json.loads(body)
    return ClaimIndex(
        frozenset(Claim(entry["kind"], entry["value"]) for entry in raw.get("claims", [])),
        {
            Claim(entry["kind"], entry["value"]): datetime.fromisoformat(entry["release_after"])
            for entry in raw.get("retained", [])
        },
    )


class ClaimStore:
    """The claim markers and the tenants' claim indexes in the tenant config bucket."""

    def __init__(self, client: Any, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def _index_key(self, tenant: str) -> str:
        return f"{OWNERS_PREFIX}{tenant}.json"

    def create(self, tenant: str, claim: Claim) -> None:
        """Create the tenant's marker on the claim, keeping the marker the tenant already has."""
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=f"{claim.prefix}{tenant}", Body=b"", IfNoneMatch="*"
            )
        except Exception as exc:
            if error_code(exc) not in S3_CONFLICT_CODES:
                raise

    def check(self, tenant: str, claim: Claim) -> ClaimCheck:
        """Return the tenants whose markers contend with the tenant's claim, and which holds it.

        This only lists digest prefixes. A channel claim also lists the markers of the other
        Slack channel kind on the same ID, since a shared app channel and an own app channel
        contend, while two own app channels do not.
        """
        return claim_check(tenant, claim, self.contending_markers(claim))

    def contending_markers(self, claim: Claim) -> dict[str, list[Marker]]:
        """Return the markers on the claim's value of each kind that contends with it."""
        return {
            kind: claim_markers(self.client, self.bucket, kind, claim.value)
            for kind in contending_kinds(claim.kind)
        }

    def claim(self, tenant: str, claims: Iterable[Claim]) -> list[ClaimCheck]:
        """Create and check the tenant's marker on each claim, and return the checks.

        Every marker that does not hold its value is deleted, and the refusal names each claim.
        """
        checks = []
        for claim in sorted(claims):
            self.create(tenant, claim)
            checks.append(self.check(tenant, claim))
        refused = [check for check in checks if not check.held]
        for check in refused:
            self.release(tenant, check.claim)
        if refused:
            self.drop_from_index(tenant, {check.claim for check in refused})
            raise ClaimRefusedError(". ".join(map(str, refused)))
        return checks

    def drop(self, tenant: str, claim: Claim) -> None:
        """Release the tenant's marker on the claim and remove the claim from its index."""
        self.release(tenant, claim)
        self.drop_from_index(tenant, {claim})

    def drop_from_index(self, tenant: str, claims: Iterable[Claim]) -> None:
        """Remove the claims from the tenant's index, as current claims or retained channels."""
        dropped = frozenset(claims)
        index = self.read_index(tenant)
        retained = {claim: at for claim, at in index.retained.items() if claim not in dropped}
        self.write_index(tenant, index.claims - dropped, retained)

    def reconcile(
        self,
        tenant: str,
        claims: frozenset[Claim],
        held: ClaimIndex,
        *,
        published_at: datetime,
        window: timedelta,
    ) -> None:
        """Write the tenant's index after a publish of the claims over the index it held.

        A shared app channel the claims drop is retained until published_at plus window, a
        retained channel past its release time is released, and every other dropped claim is
        released at once.
        """
        leaving = held.leaving(claims)
        retained = {claim: published_at + window for claim in leaving}
        for claim, release_after in held.retained.items():
            if claim in claims:
                continue
            if release_after <= published_at:
                self.release(tenant, claim)
            else:
                retained[claim] = release_after
        for claim in sorted(held.claims - claims - leaving):
            self.release(tenant, claim)
        self.write_index(tenant, claims, retained)

    def release(self, tenant: str, claim: Claim) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=f"{claim.prefix}{tenant}")

    def index(self, tenant: str) -> frozenset[Claim]:
        """Return the claims the tenant's index holds, without its retained channels."""
        return self.read_index(tenant).claims

    def read_index(self, tenant: str) -> ClaimIndex:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._index_key(tenant))
        except Exception as exc:
            if error_code(exc) in S3_MISSING_CODES:
                return ClaimIndex()
            raise
        return _parse_index(response["Body"].read())

    def _put_index(self, tenant: str, index: ClaimIndex, **conditions: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._index_key(tenant),
            Body=_index_body(index),
            ContentType="application/json",
            **conditions,
        )

    def write_index(
        self,
        tenant: str,
        claims: Iterable[Claim],
        retained: Mapping[Claim, datetime] | None = None,
    ) -> None:
        self._put_index(tenant, ClaimIndex(frozenset(claims), dict(retained or {})))

    def extend_index(self, tenant: str, claims: Iterable[Claim]) -> ClaimIndex:
        """Add the claims to the tenant's index and return the index as it was before.

        A retained channel that the claims name again returns to a current claim. An absent index
        is created without a read, since a publisher may not list its index key and S3 then
        answers the read of an absent key with AccessDenied, not NoSuchKey.
        """
        claims = frozenset(claims)
        try:
            self._put_index(tenant, ClaimIndex(claims), IfNoneMatch="*")
            return ClaimIndex()
        except Exception as exc:
            if error_code(exc) not in S3_CONFLICT_CODES:
                raise
        current = self.read_index(tenant)
        retained = {claim: at for claim, at in current.retained.items() if claim not in claims}
        self.write_index(tenant, current.claims | claims, retained)
        return current

    def markers(self) -> list[tuple[str, str, str]]:
        """Return the kind, digest, and tenant of every marker. Only an admin may list them all."""
        paginator = self.client.get_paginator("list_objects_v2")
        return sorted(
            parsed
            for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{CLAIMS_PREFIX}/")
            for entry in page.get("Contents") or []
            if (parsed := _parse_marker(entry["Key"])) is not None
        )

    def indexes(self) -> dict[str, ClaimIndex]:
        """Return every tenant's index. Only an admin may list them all."""
        paginator = self.client.get_paginator("list_objects_v2")
        tenants = [
            entry["Key"][len(OWNERS_PREFIX) : -len(".json")]
            for page in paginator.paginate(Bucket=self.bucket, Prefix=OWNERS_PREFIX)
            for entry in page.get("Contents") or []
            if entry["Key"].endswith(".json") and "/" not in entry["Key"][len(OWNERS_PREFIX) :]
        ]
        with ThreadPoolExecutor(max_workers=INDEX_READERS) as pool:
            return dict(
                zip(sorted(tenants), pool.map(self.read_index, sorted(tenants)), strict=True)
            )
