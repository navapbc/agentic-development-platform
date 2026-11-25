"""The tenant of a caller of the posting service, from a TokenReview of its projected token.

An activity worker pod of a served tenant posts for its tenant. A pod of a platform service
account in the platform namespace, the inbound webhook or the deployment-validate Job, is named
PLATFORM_CALLER, which the service admits only for administrator posts and reads of the groups
platform-escalation.yaml names. A caller presents the service account
token that its pod mounts for the posting service audience, and the service asks the Kubernetes
API server to review it with the service's own service account credentials. Every post is
reviewed, and no review result is reused.

The bound pod name is checked as well as the service account, because a tenant's MCP gateway
runs as the same service account as its activity worker.
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

import httpx

from services.http_front_door import bearer_header
from services.orchestration.task_queues import TENANT_NAME
from services.slack_poster import SERVICE_ACCOUNT_AUDIENCE

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
REVIEW_PATH = "/apis/authentication.k8s.io/v1/tokenreviews"
REVIEW_TIMEOUT_SECONDS = 3.0

TENANT_NAMESPACE_PREFIX = "tenant-"
WORKER_SERVICE_ACCOUNT = "activity-worker"
WORKER_POD_PREFIX = "activity-worker-"
SERVICE_ACCOUNT_USER_PREFIX = "system:serviceaccount:"
POD_NAME_EXTRA = "authentication.kubernetes.io/pod-name"
POD_UID_EXTRA = "authentication.kubernetes.io/pod-uid"
PLATFORM_NAMESPACE = "agentic-dev-platform"
PLATFORM_SERVICE_ACCOUNTS = frozenset({"inbound-webhook", "deployment-validate"})
PLATFORM_CALLER = "@platform"

TokenReview = Callable[[str], Mapping[str, Any]]


class IdentityRefused(Exception):
    """The presented token does not identify an activity worker pod of a served tenant."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReviewUnavailable(Exception):
    """The Kubernetes API server could not review the token."""


class KubernetesTokenReview:
    """Review a token through the Kubernetes API with this pod's service account credentials.

    The own token is read on every review, because the kubelet replaces the projected file
    before it expires.
    """

    def __init__(self, client: httpx.Client, *, credentials_dir: Path = SERVICE_ACCOUNT_DIR):
        self._client = client
        self._own_token = credentials_dir / "token"

    @classmethod
    def in_cluster(cls, credentials_dir: Path = SERVICE_ACCOUNT_DIR) -> KubernetesTokenReview:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        authority = f"[{host}]" if ":" in host else host
        client = httpx.Client(
            base_url=f"https://{authority}:{port}",
            verify=ssl.create_default_context(cafile=credentials_dir / "ca.crt"),
            timeout=REVIEW_TIMEOUT_SECONDS,
        )
        return cls(client, credentials_dir=credentials_dir)

    def __call__(self, token: str) -> Mapping[str, Any]:
        """Return the review's status for the posting service audience."""
        review = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "spec": {"token": token, "audiences": [SERVICE_ACCOUNT_AUDIENCE]},
        }
        try:
            own_token = self._own_token.read_text(encoding="utf-8").strip()
            response = self._client.post(REVIEW_PATH, json=review, headers=bearer_header(own_token))
        except (OSError, httpx.HTTPError) as exc:
            raise ReviewUnavailable(
                f"the TokenReview request failed: {type(exc).__name__}"
            ) from exc
        if not response.is_success:
            raise ReviewUnavailable(f"the TokenReview request returned {response.status_code}")
        try:
            status = response.json().get("status")
        except ValueError, AttributeError:
            status = None
        if not isinstance(status, Mapping):
            raise ReviewUnavailable("the TokenReview response has no status")
        return status


class WorkerIdentity:
    """Name the tenant whose activity worker pod presented a token, or PLATFORM_CALLER for a pod
    of a platform service account, or refuse the token."""

    def __init__(self, review: TokenReview, tenants: Collection[str]) -> None:
        self._review = review
        self._tenants = frozenset(tenants)

    def tenant(self, token: str) -> str:
        status = self._review(token)
        if status.get("authenticated") is not True:
            raise IdentityRefused("the token is not authenticated")
        if SERVICE_ACCOUNT_AUDIENCE not in (status.get("audiences") or ()):
            raise IdentityRefused("the review does not carry the posting service audience")
        user = status.get("user") or {}
        username = str(user.get("username") or "")
        platform_account = _platform_account(username)
        tenant = PLATFORM_CALLER if platform_account else self._worker_tenant(username)
        extra = user.get("extra") or {}
        if not _single(extra.get(POD_UID_EXTRA)):
            raise IdentityRefused("the token is not bound to a pod")
        pod = _single(extra.get(POD_NAME_EXTRA))
        if platform_account:
            if not pod.startswith(f"{platform_account}-"):
                raise IdentityRefused("the token is not bound to a pod of its service account")
        elif not pod.startswith(WORKER_POD_PREFIX):
            raise IdentityRefused("the token is not bound to an activity worker pod")
        return tenant

    def _worker_tenant(self, username: str) -> str:
        if not username.startswith(SERVICE_ACCOUNT_USER_PREFIX):
            raise IdentityRefused("the caller is not a service account")
        namespace, _, account = username.removeprefix(SERVICE_ACCOUNT_USER_PREFIX).partition(":")
        if account != WORKER_SERVICE_ACCOUNT:
            raise IdentityRefused("the caller is not the activity worker service account")
        tenant = namespace.removeprefix(TENANT_NAMESPACE_PREFIX)
        if (
            not namespace.startswith(TENANT_NAMESPACE_PREFIX)
            or not TENANT_NAME.fullmatch(tenant)
            or tenant not in self._tenants
        ):
            raise IdentityRefused("the caller's namespace is not a served tenant's namespace")
        return tenant


def _platform_account(username: str) -> str:
    namespace, _, account = username.removeprefix(SERVICE_ACCOUNT_USER_PREFIX).partition(":")
    if (
        username.startswith(SERVICE_ACCOUNT_USER_PREFIX)
        and namespace == PLATFORM_NAMESPACE
        and account in PLATFORM_SERVICE_ACCOUNTS
    ):
        return account
    return ""


def _single(values: object) -> str:
    if isinstance(values, list) and len(values) == 1 and isinstance(values[0], str):
        return values[0]
    return ""
