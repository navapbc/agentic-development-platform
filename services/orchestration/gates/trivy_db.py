"""Deliver Trivy's vulnerability database to scanner pods that cannot download it.

A scanner pod on EKS reaches no public registry, and it removes its ECR credential before the
scanner runs. The activity worker pulls the database through the ghcr.io pull-through cache with
its own ECR token and keeps the compressed layer by digest. Each scan copies that layer into the
pod, which unpacks it for a Trivy run that downloads nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from services.orchestration.gates import registry_auth, registry_mirrors

GHCR = "ghcr.io"
REPOSITORY = "aquasecurity/trivy-db"
TAG = "2"
POD_DIR = "/trivy-cache"
ARCHIVE = "db.tar.gz"
REFRESH_SECONDS = 6 * 3600
_KEPT = 2
_LAYER_TYPE = "application/vnd.aquasec.trivy.db.layer.v1.tar+gzip"
_MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
_CURRENT = "current"
_PARTIAL = ".partial-"
_CHUNK = 1 << 20


class TrivyDatabaseError(RuntimeError):
    """The database could not be resolved or downloaded through the mirror."""


def mirrored_repository(config: registry_mirrors.RegistryConfig | None) -> tuple[str, str] | None:
    """The registry host and repository that mirror trivy-db, or None when ghcr.io has no mirror."""
    location = config.mirrors.get(GHCR) if config is not None else None
    if not location:
        return None
    host, _, prefix = location.partition("/")
    return host, "/".join(part for part in (prefix, REPOSITORY) if part)


class TrivyDatabase:
    """The newest trivy-db layer, resolved through the mirror at most every REFRESH_SECONDS.

    Every worker slot of a tenant shares `root`. A download lands in a private directory and
    moves into place whole, and pruning keeps the previous layer, which another slot may still be
    copying into a pod.
    """

    def __init__(
        self,
        host: str,
        repository: str,
        root: Path,
        *,
        token: registry_auth.TokenSource | None = None,
        clock: Callable[[], float] = time.time,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._host = host
        self._repository = repository
        self._root = root
        self._token = token or registry_auth.ecr_token
        self._clock = clock
        self._transport = transport

    async def directories(self) -> Mapping[str, str]:
        return {POD_DIR: str(await asyncio.to_thread(self.current))}

    def prepare(self) -> str:
        """Unpack the layer in the pod where the scanner's cache directory expects it."""
        archive = shlex.quote(f"{POD_DIR}/{ARCHIVE}")
        db = shlex.quote(f"{POD_DIR}/db")
        return f"mkdir -p {db} && tar -xzf {archive} -C {db} && rm -f {archive}"

    def current(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        cached = self._recent()
        if cached is not None:
            return cached
        with httpx.Client(
            headers={"Authorization": f"Basic {self._registry_token()}"},
            timeout=httpx.Timeout(60, read=300),
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            digest = self._layer_digest(client)
            target = self._root / digest.replace(":", "-")
            if not (target / ARCHIVE).is_file():
                self._download(client, digest, target)
        self._record(target.name)
        self._prune(keep=target.name)
        return target

    def _recent(self) -> Path | None:
        marker = self._root / _CURRENT
        try:
            age = self._clock() - marker.stat().st_mtime
            name = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        target = self._root / name
        if name and age < REFRESH_SECONDS and (target / ARCHIVE).is_file():
            return target
        return None

    def _registry_token(self) -> str:
        where = registry_auth.ecr_registries([f"{self._host}/{self._repository}"]).get(self._host)
        if where is None:
            raise TrivyDatabaseError(f"{self._host} is not an ECR registry")
        return self._token(*where)

    def _layer_digest(self, client: httpx.Client) -> str:
        url = f"https://{self._host}/v2/{self._repository}/manifests/{TAG}"
        response = client.get(url, headers={"Accept": _MANIFEST_TYPE})
        if response.status_code != httpx.codes.OK:
            raise TrivyDatabaseError(
                f"{url} answered {response.status_code}: {response.text.strip()[:300]}"
            )
        for layer in response.json().get("layers") or []:
            if layer.get("mediaType") == _LAYER_TYPE:
                return str(layer["digest"])
        raise TrivyDatabaseError(f"{url} lists no {_LAYER_TYPE} layer")

    def _download(self, client: httpx.Client, digest: str, target: Path) -> None:
        algorithm, _, expected = digest.partition(":")
        staging = Path(tempfile.mkdtemp(prefix=_PARTIAL, dir=self._root))
        try:
            sha = hashlib.new(algorithm)
            url = f"https://{self._host}/v2/{self._repository}/blobs/{digest}"
            with client.stream("GET", url) as response, (staging / ARCHIVE).open("wb") as out:
                if response.status_code != httpx.codes.OK:
                    raise TrivyDatabaseError(f"{url} answered {response.status_code}")
                for chunk in response.iter_bytes(_CHUNK):
                    sha.update(chunk)
                    out.write(chunk)
            if sha.hexdigest() != expected:
                raise TrivyDatabaseError(f"the layer downloaded from {url} does not match {digest}")
            try:
                os.replace(staging, target)
            except OSError:
                if not (target / ARCHIVE).is_file():
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _record(self, name: str) -> None:
        marker = self._root / _CURRENT
        handle, path = tempfile.mkstemp(prefix=_PARTIAL, dir=self._root)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(name)
        os.replace(path, marker)

    def _prune(self, *, keep: str) -> None:
        layers = sorted(
            (p for p in self._root.glob("sha256-*") if p.is_dir() and p.name != keep),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in layers[_KEPT - 1 :]:
            shutil.rmtree(stale, ignore_errors=True)


def database_for(settings) -> TrivyDatabase | None:
    """The database scanner pods receive in this context, or None when Trivy downloads its own."""
    mirrored = mirrored_repository(registry_mirrors.current())
    if mirrored is None:
        return None
    root = Path(settings.execution_workspace_root) / ".trivy-db"
    return TrivyDatabase(*mirrored, root)
