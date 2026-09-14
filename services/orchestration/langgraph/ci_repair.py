"""Bounded LangGraph stage that classifies a CI failure and plans a single repair instruction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

ACTIONABLE = "actionable"
UNREPAIRABLE = "unrepairable"

_FAILURE_MARKERS = (
    "fail",
    "error",
    "traceback",
    "assert",
    "reformat",
    "severity",
    "ruff",
    "bandit",
    "pytest",
    "security",
    "issue",
    "shell=",
    # Prettier and eslint name the offending file on its own "[warn] <path>" line, which carries
    # no other marker and is what the repair agent needs to locate the change.
    "[warn]",
)


def failure_summary(log_text: str, *, max_lines: int = 20) -> str:
    """Pick the failure-relevant lines from a check log, keeping the last max_lines."""
    picked = [
        line for line in log_text.splitlines() if any(m in line.lower() for m in _FAILURE_MARKERS)
    ]
    return "\n".join(picked[-max_lines:])


TEST_FAILURE = "test_failure"
LINT = "lint"
SECURITY = "security"
BUILD = "build"
UNKNOWN = "unknown"

_TEST_MARKERS = ("AssertionError", "FAILED", "assert ", "pytest", "test failed")
_LINT_MARKERS = (
    "ruff",
    "flake8",
    "lint",
    "E501",
    "would reformat",
    "format",
    "prettier",
    "code style",
    "eslint",
    "✖",
)
_SECURITY_MARKERS = (
    ">> issue:",
    "security issue",
    "shell=true",
    "severity: high",
    "severity: medium",
)
_BUILD_MARKERS = (
    "SyntaxError",
    "ImportError",
    "ModuleNotFoundError",
    "NameError",
    "BUILD FAILURE",
    "compilation",
    # tsc reports type errors as "file.ts(1,2): error TS2322: ...".
    "error TS",
)

_INSTRUCTIONS = {
    TEST_FAILURE: (
        "The CI build failed on a test. Read the failing test output below, make the smallest "
        "change that makes the test pass without weakening the assertion, and do not edit the "
        "test to hide the failure.\n\n{summary}"
    ),
    LINT: (
        "The CI build failed on a lint or format check. Fix the violations reported below "
        "without changing behaviour, using the project's own tooling rather than a formatter "
        "from another language's ecosystem. Change only the files named in the report."
        "{fix_hint}\n\n{summary}"
    ),
    SECURITY: (
        "The CI build failed a security scan. Fix the security finding reported below by "
        "removing the unsafe code, not by suppressing it, and do not add nosec, noqa, or ignore "
        "comments.\n\n{summary}"
    ),
    BUILD: (
        "The CI build failed to build or import. Fix the syntax, import, or name error reported "
        "below.\n\n{summary}"
    ),
}


@dataclass(frozen=True)
class RepairPlan:
    status: str
    category: str
    instruction: str


def classify_failure(failure_summary: str) -> str:
    text = failure_summary.lower()
    if any(marker.lower() in text for marker in _BUILD_MARKERS):
        return BUILD
    if any(marker.lower() in text for marker in _SECURITY_MARKERS):
        return SECURITY
    if any(marker.lower() in text for marker in _TEST_MARKERS):
        return TEST_FAILURE
    if any(marker.lower() in text for marker in _LINT_MARKERS):
        return LINT
    return UNKNOWN


class _RepairState(TypedDict):
    summary: str
    category: str
    status: str
    instruction: str
    fix_command: str


def _classify(state: _RepairState) -> _RepairState:
    if not state["summary"].strip():
        return {**state, "category": UNKNOWN}
    return {**state, "category": classify_failure(state["summary"])}


def _route(state: _RepairState) -> str:
    if not state["summary"].strip() or state["category"] == UNKNOWN:
        return "abort"
    return "plan"


def _plan(state: _RepairState) -> _RepairState:
    template = _INSTRUCTIONS[state["category"]]
    fix_command = state.get("fix_command") or ""
    fix_hint = (
        f" The project declares its own fix command, so run `{fix_command}` before editing by hand."
        if fix_command
        else ""
    )
    return {
        **state,
        "status": ACTIONABLE,
        "instruction": template.format(
            summary=state["summary"].strip(), fix_hint=fix_hint
        ),
    }


_PATH_PATTERN = re.compile(r"[\w./\\@-]+\.[A-Za-z][A-Za-z0-9]{0,5}")
_PATH_NOISE = ("node_modules/", ".git/")


def failing_files(summary: str) -> tuple[str, ...]:
    """Extract the source paths a check named, used to bound what a repair may touch."""
    found: list[str] = []
    for raw in _PATH_PATTERN.findall(summary or ""):
        candidate = raw.strip().strip("'\"`,;:()[]").lstrip("./")
        if "/" not in candidate and "." not in candidate:
            continue
        if not candidate or any(noise in candidate for noise in _PATH_NOISE):
            continue
        if candidate.startswith("-") or candidate.endswith("."):
            continue
        if candidate not in found:
            found.append(candidate)
    return tuple(found)


def in_repair_scope(changed_path: str, scope: tuple[str, ...]) -> bool:
    """A changed file is in scope when it matches a path the failure named.

    Paths are compared by suffix because a check reports them relative to its own working
    directory while the workspace sees them relative to the repository root.
    """
    if not scope:
        return True
    normalised = changed_path.lstrip("./")
    for entry in scope:
        if normalised == entry or normalised.endswith("/" + entry) or entry.endswith(
            "/" + normalised
        ):
            return True
    return False


def _abort(state: _RepairState) -> _RepairState:
    return {**state, "status": UNREPAIRABLE, "instruction": ""}


def build_repair_graph():
    graph = StateGraph(_RepairState)
    graph.add_node("classify", _classify)
    graph.add_node("plan", _plan)
    graph.add_node("abort", _abort)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", _route, {"plan": "plan", "abort": "abort"})
    graph.add_edge("plan", END)
    graph.add_edge("abort", END)
    return graph.compile()


_GRAPH = build_repair_graph()


def plan_repair(failure_summary: str, *, fix_command: str = "") -> RepairPlan:
    final = _GRAPH.invoke(
        {
            "summary": failure_summary,
            "category": "",
            "status": "",
            "instruction": "",
            "fix_command": fix_command,
        }
    )
    return RepairPlan(
        status=final["status"],
        category=final["category"],
        instruction=final["instruction"],
    )


_MAX_SUMMARY_CHARS = 8000


def _repair_llm_config(settings=None):
    from services.model_gateway.llm_config import llm_config_for

    return llm_config_for("repair-model", settings)


def _model_instruction(failure_summary: str, category: str, *, runner=None, settings=None) -> str:
    from observability import bind_execution
    from rebar.llm.runner import RunRequest, get_runner

    from services.prompts import load_prompt

    cfg = _repair_llm_config(settings)
    run = runner or get_runner(cfg)
    loaded = load_prompt("ci-repair")
    with bind_execution(stage="repair", prompt_version=loaded.version):
        result = run.run(
            RunRequest(
                system_prompt=loaded.text,
                instructions=(
                    f"Failure category: {category}.\n"
                    f"Failing job log:\n{failure_summary[:_MAX_SUMMARY_CHARS]}"
                ),
                config=cfg,
                reviewers=[],
            )
        )
    return str(result.get("summary") or "").strip()


def analyze_failure(
    failure_summary: str, *, use_model: bool, runner=None, settings=None, fix_command: str = ""
) -> RepairPlan:
    base = plan_repair(failure_summary, fix_command=fix_command)
    if base.status != ACTIONABLE or not use_model:
        return base
    try:
        instruction = _model_instruction(
            failure_summary, base.category, runner=runner, settings=settings
        )
    except Exception:  # noqa: BLE001
        return base
    return RepairPlan(
        status=ACTIONABLE,
        category=base.category,
        instruction=instruction or base.instruction,
    )
