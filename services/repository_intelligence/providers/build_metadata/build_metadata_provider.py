"""Build-metadata repository-intelligence provider.

Parses Python build files (`pyproject.toml`, `requirements*.txt`) and Terraform
configuration (`*.tf` `required_providers` and `module` blocks) into dependency
edges and build/test targets. Backs `repo_get_module_dependencies` with
authoritative Source.BUILD_METADATA edges for declared third-party packages and
Terraform providers and modules, which the symbol graph does not capture, and
exposes build/test targets that feed change-impact and context-package assembly.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from services.repository_intelligence.normalization.models import (
    DependencyEdge,
    Provenance,
    RepoRef,
    Source,
)

_BUILD_METADATA_CONFIDENCE = 1.0

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_DEV_STEM = re.compile(r".*[-_](dev|test|tests|lint|typing)$")
_NORMALIZE = re.compile(r"[-_.]+")

_TF_SOURCE = re.compile(r'^\s*source\s*=\s*"([^"]+)"')
_TF_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_TF_LEAD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)")
_TF_LOCAL_PREFIXES = ("./", "../")

_KIND_RANK = {
    "runtime_dependency": 0,
    "build_dependency": 1,
    "dev_dependency": 3,
}


def _kind_rank(kind: str) -> int:
    if kind.startswith("optional_dependency"):
        return 2
    return _KIND_RANK.get(kind, 4)


@dataclass(frozen=True)
class BuildTarget:
    name: str
    kind: str
    value: str


def canonical_name(name: str) -> str:
    """Return the PEP 503 normalized distribution name."""
    return _NORMALIZE.sub("-", name).lower()


class BuildMetadataProvider:
    """Answers dependency and build/test-target queries from declared build files."""

    def __init__(self, repo_root: str | Path, *, project: str | None = None) -> None:
        self._root = Path(repo_root).resolve()
        self._project = project or self._root.name

    def repo_get_module_dependencies(self, repo: RepoRef, module: str = "") -> list[DependencyEdge]:
        if module and canonical_name(module) != canonical_name(self._project):
            return []
        provenance = Provenance(source=Source.BUILD_METADATA, confidence=_BUILD_METADATA_CONFIDENCE)
        best: dict[str, str] = {}
        for name, kind in self._declared_dependencies():
            current = best.get(name)
            if current is None or _kind_rank(kind) < _kind_rank(current):
                best[name] = kind
        edges = [
            DependencyEdge(
                from_module=self._project,
                to_module=name,
                kind=kind,
                provenance=provenance,
            )
            for name, kind in best.items()
        ]
        return sorted(edges, key=lambda edge: (edge.to_module, edge.kind))

    def build_targets(self) -> list[BuildTarget]:
        targets: list[BuildTarget] = []
        data = self._read_pyproject()
        build_system = data.get("build-system") or {}
        backend = build_system.get("build-backend")
        if isinstance(backend, str):
            targets.append(BuildTarget(name=backend, kind="build_backend", value=backend))
        scripts = (data.get("project") or {}).get("scripts") or {}
        for name, entry in scripts.items():
            if isinstance(entry, str):
                targets.append(BuildTarget(name=name, kind="console_script", value=entry))
        pytest_config = ((data.get("tool") or {}).get("pytest") or {}).get("ini_options") or {}
        for path in self._as_list(pytest_config.get("testpaths")):
            targets.append(BuildTarget(name=path, kind="test", value=path))
        return sorted(targets, key=lambda target: (target.kind, target.name))

    def _declared_dependencies(self) -> list[tuple[str, str]]:
        deps: list[tuple[str, str]] = []
        deps.extend(self._pyproject_dependencies())
        for path in sorted(self._root.glob("requirements*.txt")):
            kind = "dev_dependency" if _DEV_STEM.match(path.stem) else "runtime_dependency"
            deps.extend((name, kind) for name in self._parse_requirements(path, set()))
        deps.extend(self._terraform_dependencies())
        return deps

    def _terraform_dependencies(self) -> list[tuple[str, str]]:
        deps: list[tuple[str, str]] = []
        for path in sorted(self._root.rglob("*.tf")):
            if ".terraform" in path.parts:
                continue
            deps.extend(self._parse_terraform(path))
        return deps

    @staticmethod
    def _parse_terraform(path: Path) -> list[tuple[str, str]]:
        deps: list[tuple[str, str]] = []
        stack: list[str] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            source = _TF_SOURCE.match(raw)
            if source is not None:
                value = source.group(1)
                if "required_providers" in stack:
                    deps.append((value, "provider_dependency"))
                elif "module" in stack:
                    kind = (
                        "local_module_dependency"
                        if value.startswith(_TF_LOCAL_PREFIXES)
                        else "module_dependency"
                    )
                    deps.append((value, kind))
            stripped = _TF_STRING.sub('""', raw).split("#", 1)[0].split("//", 1)[0]
            opens = stripped.count("{")
            closes = stripped.count("}")
            if opens > closes:
                lead = _TF_LEAD.match(raw)
                tag = lead.group(1) if lead else "block"
                stack.extend([tag] * (opens - closes))
            elif closes > opens:
                for _ in range(closes - opens):
                    if stack:
                        stack.pop()
        return deps

    def _pyproject_dependencies(self) -> list[tuple[str, str]]:
        data = self._read_pyproject()
        project = data.get("project") or {}
        deps: list[tuple[str, str]] = []
        for spec in project.get("dependencies") or []:
            name = self._requirement_name(spec)
            if name:
                deps.append((name, "runtime_dependency"))
        for extra, specs in (project.get("optional-dependencies") or {}).items():
            for spec in specs or []:
                name = self._requirement_name(spec)
                if name:
                    deps.append((name, f"optional_dependency:{extra}"))
        for spec in (data.get("build-system") or {}).get("requires") or []:
            name = self._requirement_name(spec)
            if name:
                deps.append((name, "build_dependency"))
        return deps

    def _parse_requirements(self, path: Path, visited: set[Path]) -> list[str]:
        resolved = path.resolve()
        if resolved in visited or not resolved.is_file():
            return []
        visited.add(resolved)
        names: list[str] = []
        for raw in resolved.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith(("-r ", "--requirement ")):
                target = line.split(maxsplit=1)[1].strip()
                names.extend(self._parse_requirements(resolved.parent / target, visited))
                continue
            if line.startswith("-"):
                continue
            name = self._requirement_name(line)
            if name:
                names.append(name)
        return names

    @staticmethod
    def _requirement_name(spec: str) -> str | None:
        match = _REQUIREMENT_NAME.match(spec)
        return canonical_name(match.group(1)) if match else None

    def _read_pyproject(self) -> dict:
        path = self._root / "pyproject.toml"
        if not path.is_file():
            return {}
        return tomllib.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _as_list(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        return []
