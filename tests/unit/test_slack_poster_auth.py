"""The posting service takes a caller's tenant from a TokenReview of its projected token."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from services.slack_poster import TOKEN_AUDIENCE
from services.slack_poster.auth import (
    IdentityRefused,
    KubernetesTokenReview,
    ReviewUnavailable,
    WorkerIdentity,
)

REVIEW_PATH = "/apis/authentication.k8s.io/v1/tokenreviews"
WORKER = "system:serviceaccount:tenant-adp:activity-worker"


@dataclass(frozen=True)
class _Issued:
    username: str = WORKER
    pod_name: str | None = "activity-worker-abc"
    pod_uid: str | None = "0b6c3f1e-pod"
    audiences: tuple[str, ...] = (TOKEN_AUDIENCE,)
    reported_audiences: tuple[str, ...] | None = None


class _ApiServer:
    def __init__(self, own_token: Path) -> None:
        self.own_token = own_token
        self.issued: dict[str, _Issued] = {}
        self.reviews: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != REVIEW_PATH or request.method != "POST":
            return httpx.Response(404)
        if request.headers.get("Authorization") != f"Bearer {self.own_token.read_text()}":
            return httpx.Response(401)
        body = json.loads(request.content)
        self.reviews.append(body)
        spec = body["spec"]
        issued = self.issued.get(spec["token"])
        matched = [
            audience
            for audience in spec.get("audiences", [])
            if issued is not None and audience in issued.audiences
        ]
        if issued is None or not matched:
            return httpx.Response(
                201, json={"status": {"authenticated": False, "error": "invalid bearer token"}}
            )
        extra: dict[str, list[str]] = {}
        if issued.pod_name is not None:
            extra["authentication.kubernetes.io/pod-name"] = [issued.pod_name]
        if issued.pod_uid is not None:
            extra["authentication.kubernetes.io/pod-uid"] = [issued.pod_uid]
        status = {
            "authenticated": True,
            "user": {"username": issued.username, "uid": "sa-uid", "extra": extra},
            "audiences": list(
                matched if issued.reported_audiences is None else issued.reported_audiences
            ),
        }
        return httpx.Response(201, json={"kind": "TokenReview", "status": status})


@pytest.fixture
def own_token(tmp_path) -> Path:
    path = tmp_path / "token"
    path.write_text("posting-service-own-token")
    return path


@pytest.fixture
def api(own_token) -> _ApiServer:
    return _ApiServer(own_token)


def _identity(api: _ApiServer, tenants: tuple[str, ...] = ("adp",)) -> WorkerIdentity:
    client = httpx.Client(
        transport=httpx.MockTransport(api.handle), base_url="https://kubernetes.test"
    )
    review = KubernetesTokenReview(client, credentials_dir=api.own_token.parent)
    return WorkerIdentity(review, tenants)


def test_activity_worker_pod_of_a_served_tenant_is_that_tenant(api):
    api.issued["worker"] = _Issued()

    assert _identity(api).tenant("worker") == "adp"
    assert api.reviews[0]["spec"] == {"token": "worker", "audiences": [TOKEN_AUDIENCE]}


def test_each_post_is_reviewed_with_the_current_own_token(api, own_token):
    api.issued["worker"] = _Issued()
    identity = _identity(api)

    identity.tenant("worker")
    own_token.write_text("rotated-own-token")
    identity.tenant("worker")

    assert len(api.reviews) == 2


@pytest.mark.parametrize(
    "issued",
    [
        pytest.param(_Issued(pod_name="mcp-gateway-xyz"), id="mcp-gateway-pod"),
        pytest.param(_Issued(pod_name=None), id="no-pod-name"),
        pytest.param(
            _Issued(username="system:serviceaccount:tenant-adp-sandbox:activity-worker"),
            id="sandbox-namespace",
        ),
        pytest.param(
            _Issued(username="system:serviceaccount:tenant-adp:mcp-gateway"),
            id="other-service-account",
        ),
        pytest.param(
            _Issued(username="system:serviceaccount:tenant-beta:activity-worker"),
            id="tenant-not-served",
        ),
        pytest.param(_Issued(username="system:node:worker-1"), id="not-a-service-account"),
        pytest.param(_Issued(audiences=("https://kubernetes.default.svc",)), id="other-audience"),
        pytest.param(_Issued(reported_audiences=()), id="status-lacks-audience"),
        pytest.param(_Issued(pod_uid=None), id="no-pod-uid"),
    ],
)
def test_a_caller_other_than_a_served_tenant_activity_worker_is_refused(api, issued):
    api.issued["caller"] = issued

    with pytest.raises(IdentityRefused):
        _identity(api).tenant("caller")


def test_a_token_the_api_server_did_not_issue_is_refused(api):
    with pytest.raises(IdentityRefused):
        _identity(api).tenant("forged")


def test_a_sandbox_namespace_is_refused_even_when_its_suffix_is_a_listed_tenant(api):
    api.issued["sandbox"] = _Issued(
        username="system:serviceaccount:tenant-x-sandbox:activity-worker"
    )

    with pytest.raises(IdentityRefused):
        _identity(api, tenants=("x", "x-sandbox")).tenant("sandbox")


def test_an_unreachable_api_server_makes_the_review_unavailable(own_token):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(
        transport=httpx.MockTransport(unreachable), base_url="https://kubernetes.test"
    )
    identity = WorkerIdentity(
        KubernetesTokenReview(client, credentials_dir=own_token.parent), ("adp",)
    )

    with pytest.raises(ReviewUnavailable):
        identity.tenant("worker")
