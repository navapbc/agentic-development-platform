"""The eks-deployer image holds every module its scripts import, beside the standard library."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
DOCKERFILE = REPOSITORY / "docker" / "eks-deployer.Dockerfile"
PACKAGES = ("scripts", "services")


def _copied_modules() -> set[str]:
    modules: set[str] = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"COPY (?!--from)(.+) /opt/eks-deployer/(scripts|services)/$", line)
        if match:
            modules.update(
                source.removesuffix(".py").replace("/", ".")
                for source in match.group(1).split()
                if source.endswith(".py")
            )
    return modules


def _runtime_imports(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and getattr(node.test, "id", "") == "TYPE_CHECKING":
            node.body = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_every_import_of_the_copied_scripts_is_copied_or_in_the_standard_library() -> None:
    copied = _copied_modules()
    assert "scripts.eks_deploy_task" in copied

    def available(name: str) -> bool:
        top = name.split(".")[0]
        if top in PACKAGES:
            return name in copied or name == top
        return top in sys.stdlib_module_names

    missing = sorted(
        f"{module} imports {name}"
        for module in copied
        for name in _runtime_imports(
            ast.parse((REPOSITORY / f"{module.replace('.', '/')}.py").read_text(encoding="utf-8"))
        )
        if not available(name)
    )

    assert missing == []
