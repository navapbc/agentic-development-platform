"""The activity worker delivers Trivy's database to scanner pods through the ghcr.io mirror."""

from __future__ import annotations

import hashlib
import os
import time

import httpx
import pytest
from services.orchestration.gates.registry_mirrors import RegistryConfig
from services.orchestration.gates.trivy_db import (
    ARCHIVE,
    REFRESH_SECONDS,
    TrivyDatabase,
    TrivyDatabaseError,
    mirrored_repository,
)

HOST = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
REPOSITORY = "p-ghcr/aquasecurity/trivy-db"


class _Registry:
    def __init__(self, layer: bytes, *, digest: str | None = None) -> None:
        self.layer = layer
        self.digest = digest or f"sha256:{hashlib.sha256(layer).hexdigest()}"
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        assert request.headers["Authorization"] == "Basic ecr-token"
        if request.url.path.endswith("/manifests/2"):
            layer = {"mediaType": "application/vnd.aquasec.trivy.db.layer.v1.tar+gzip"}
            return httpx.Response(200, json={"layers": [{**layer, "digest": self.digest}]})
        if request.url.path.endswith(f"/blobs/{self.digest}"):
            return httpx.Response(200, content=self.layer)
        return httpx.Response(404)


def _database(registry: _Registry, root, clock=lambda: 1_000_000.0) -> TrivyDatabase:
    return TrivyDatabase(
        HOST,
        REPOSITORY,
        root,
        token=lambda account, region: "ecr-token",
        clock=clock,
        transport=httpx.MockTransport(registry.handler),
    )


def test_the_mirror_of_ghcr_io_names_the_trivy_db_repository():
    config = RegistryConfig(mode="aws", mirrors={"ghcr.io": f"{HOST}/p-ghcr"})

    assert mirrored_repository(config) == (HOST, REPOSITORY)
    assert mirrored_repository(RegistryConfig(mode="aws")) is None
    assert mirrored_repository(None) is None


def test_the_layer_is_downloaded_once_and_reused_until_the_refresh(tmp_path):
    registry = _Registry(b"layer-one")
    now = [1_000_000.0]

    first = _database(registry, tmp_path, clock=lambda: now[0]).current()
    os.utime(tmp_path / "current", (now[0], now[0]))
    again = _database(registry, tmp_path, clock=lambda: now[0] + 60).current()

    assert (first / ARCHIVE).read_bytes() == b"layer-one"
    assert again == first
    assert sum(path.endswith("/manifests/2") for path in registry.requests) == 1

    now[0] += REFRESH_SECONDS + 1
    refreshed = _database(registry, tmp_path, clock=lambda: now[0]).current()

    assert refreshed == first
    assert sum("/blobs/" in path for path in registry.requests) == 1


def test_a_new_layer_replaces_the_old_and_the_previous_one_is_kept(tmp_path):
    def clock() -> float:
        return time.time() + 2 * REFRESH_SECONDS

    paths = [_database(_Registry(body), tmp_path, clock=clock).current() for body in (b"a", b"b")]
    _database(_Registry(b"c"), tmp_path, clock=clock).current()

    layers = sorted(p.name for p in tmp_path.glob("sha256-*"))
    assert paths[0].name not in layers
    assert paths[1].name in layers and len(layers) == 2


def test_a_layer_that_does_not_match_its_digest_is_refused(tmp_path):
    registry = _Registry(b"tampered", digest=f"sha256:{hashlib.sha256(b'expected').hexdigest()}")

    with pytest.raises(TrivyDatabaseError, match="does not match"):
        _database(registry, tmp_path).current()
    assert not list(tmp_path.glob("sha256-*"))
