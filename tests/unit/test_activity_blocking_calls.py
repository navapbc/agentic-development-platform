"""No async activity blocks the worker's event loop with a direct blocking call.

Every worker heartbeats its running activities from the event loop, so a call that blocks the
loop for longer than the heartbeat timeout times the activity out while it still runs, and
Temporal starts a second attempt beside it. Such a call belongs in asyncio.to_thread.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_BLOCKING = {
    ("process", "run"),
    ("process", "popen"),
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("subprocess", "Popen"),
    ("time", "sleep"),
    ("requests", "get"),
    ("requests", "post"),
    ("requests", "request"),
}


def _is_activity(node: ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "defn"
            and isinstance(target.value, ast.Name)
            and target.value.id == "activity"
        ):
            return True
    return False


def _blocking_calls(node: ast.AsyncFunctionDef) -> list[str]:
    found = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        owner = call.func.value
        if isinstance(owner, ast.Name) and (owner.id, call.func.attr) in _BLOCKING:
            found.append(f"{owner.id}.{call.func.attr} at line {call.lineno}")
    return found


def test_no_async_activity_calls_a_blocking_function_directly() -> None:
    from services.orchestration.worker import _all_activities

    offenders = []
    activities = 0
    for root in ("services", "openhands"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef) and _is_activity(node):
                    activities += 1
                    offenders += [
                        f"{path.relative_to(REPO_ROOT)} {node.name}: {call}"
                        for call in _blocking_calls(node)
                    ]

    assert activities >= len(_all_activities())
    assert offenders == []


def test_the_check_finds_a_blocking_call_inside_an_activity() -> None:
    source = (
        "from temporalio import activity\n"
        "import time\n"
        "@activity.defn\n"
        "async def slow() -> None:\n"
        "    time.sleep(600)\n"
    )
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef))

    assert _is_activity(node)
    assert _blocking_calls(node) == ["time.sleep at line 5"]
