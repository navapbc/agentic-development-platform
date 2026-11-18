"""The images a Dockerfile build pulls, read before the build so each one is admitted first.

A builder pod pulls every `FROM` base image and every image a `COPY --from` or a
`RUN --mount=...,from=` names. Each one needs an `@sha256:` digest and passes the image policy
and the scan gate like any image a pod names, so the build refuses an image the pod could not
run. Only the global `ARG` defaults reach a `FROM` line, and the platform passes no build
arguments, so a reference that needs any other value cannot be resolved and is refused.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from services.orchestration.gates.image_admission import ImageRefusedError

# buildah bud reads the first of these a build context holds.
DEFAULT_DOCKERFILES = ("Containerfile", "Dockerfile")
_SCRATCH = "scratch"
_VARIABLE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_]\w*)(?P<op>:-)?(?P<word>[^}]*)\}|(?P<bare>[A-Za-z_]\w*))"
)
# Parser directives are comments of the form `# key=value` before any other line.
_DIRECTIVE = re.compile(r"#\s*(?P<key>[A-Za-z]+)\s*=\s*(?P<value>\S*)\s*$")


def dockerfile_for(context: Path, name: str = "") -> Path:
    """Return the Dockerfile a build of context reads, refusing a path outside context."""
    root = context.resolve()
    if name:
        path = (root / name.lstrip("/")).resolve()
        if not path.is_relative_to(root):
            raise ImageRefusedError(f"the Dockerfile {name} is outside the build context")
        if not path.is_file():
            raise ImageRefusedError(f"the Dockerfile {name} does not exist")
        return path
    for candidate in DEFAULT_DOCKERFILES:
        if (root / candidate).is_file():
            return root / candidate
    raise ImageRefusedError(f"the build context {context.name} holds no Dockerfile")


def base_images(dockerfile: Path) -> list[str]:
    """Return every image reference the build of dockerfile pulls, in the order it names them.

    Raises ImageRefusedError for a reference without a digest or one that cannot be resolved.
    """
    label = dockerfile.name
    global_args: dict[str, str | None] = {}
    stages: set[str] = set()
    images: list[str] = []
    seen_from = False
    for number, instruction, arguments in _instructions(dockerfile.read_text(encoding="utf-8")):
        where = f"{label} line {number}"
        if instruction == "ARG" and not seen_from:
            for declaration in _split(arguments, where):
                name, _, default = declaration.partition("=")
                global_args[name] = default if "=" in declaration else None
        elif instruction == "FROM":
            seen_from = True
            words = [word for word in _split(arguments, where) if not word.startswith("--")]
            if not words:
                raise ImageRefusedError(f"{where} names no image")
            reference = _substitute(words[0], global_args, where)
            if len(words) >= 3 and words[1].upper() == "AS":
                stages.add(words[2].lower())
            _collect(reference, stages, images, where)
        elif instruction in ("COPY", "RUN"):
            for source in _mount_sources(instruction, arguments, where):
                _collect(source, stages, images, where)
    return images


def _collect(reference: str, stages: set[str], images: list[str], where: str) -> None:
    if reference.lower() in stages or reference.lower() == _SCRATCH or reference.isdigit():
        return
    if "@sha256:" not in reference:
        raise ImageRefusedError(
            f"{where} pulls {reference}, which carries no @sha256: digest, which every image needs"
        )
    if reference not in images:
        images.append(reference)


def _mount_sources(instruction: str, arguments: str, where: str) -> list[str]:
    sources = []
    for word in _split(arguments, where):
        if not word.startswith("--"):
            break
        if instruction == "COPY" and word.startswith("--from="):
            sources.append(word.removeprefix("--from="))
        if instruction == "RUN" and word.startswith("--mount="):
            options = dict(
                option.partition("=")[::2] for option in word.removeprefix("--mount=").split(",")
            )
            if options.get("from"):
                sources.append(options["from"])
    return sources


def _substitute(reference: str, arguments: dict[str, str | None], where: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare")
        if match.group("word") and not match.group("op"):
            raise ImageRefusedError(f"{where} uses a substitution the build cannot resolve")
        value = arguments.get(name)
        if value:
            return value
        if match.group("op"):
            return match.group("word")
        raise ImageRefusedError(
            f"{where} names its image through ${name}, which has no default the build can use"
        )

    return _VARIABLE.sub(replace, reference)


def _split(arguments: str, where: str) -> list[str]:
    try:
        return shlex.split(arguments)
    except ValueError as exc:
        raise ImageRefusedError(f"{where} cannot be parsed: {exc}") from exc


def _instructions(text: str) -> list[tuple[int, str, str]]:
    """Return each instruction with its first line number, joining continued lines."""
    escape = "\\"
    lines = text.splitlines()
    for line in lines:
        directive = _DIRECTIVE.match(line.strip())
        if directive is None:
            break
        if directive.group("key").lower() == "escape":
            escape = directive.group("value")[:1] or escape
    instructions: list[tuple[int, str, str]] = []
    pending: list[str] = []
    start = 0
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if pending and stripped.startswith("#"):
            continue
        if not pending:
            start = number
        if stripped.endswith(escape):
            pending.append(stripped[: -len(escape)])
            continue
        pending.append(stripped)
        joined = " ".join(part for part in pending if part)
        pending = []
        instruction, _, arguments = joined.partition(" ")
        instructions.append((start, instruction.upper(), arguments.strip()))
    return instructions
