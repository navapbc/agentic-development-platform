"""Parse SARIF 2.1.0 tool output onto the shared ScanFinding severity scale.

SARIF is the native output of Semgrep, Snyk, CodeQL, Trivy, and most static and
dependency scanners, so a gate can run any SARIF-emitting tool with no bespoke parser.
Severity resolves from the numeric ``security-severity`` property when a tool supplies
it (the CVSS-style 0-10 scale), and otherwise from the SARIF result ``level``.
"""

from __future__ import annotations

import uuid
from typing import Any

from adapters.interfaces.security_scanner import (
    CRITICAL,
    HIGH,
    INFO,
    LOW,
    MEDIUM,
    STATIC,
    UNKNOWN,
    ScanFinding,
)

_LEVEL_SEVERITY = {
    "error": HIGH,
    "warning": MEDIUM,
    "note": LOW,
    "none": INFO,
}


def _severity_from_score(score: float) -> str:
    if score >= 9.0:
        return CRITICAL
    if score >= 7.0:
        return HIGH
    if score >= 4.0:
        return MEDIUM
    if score > 0.0:
        return LOW
    return INFO


def _security_severity(*sources: dict[str, Any]) -> str | None:
    for source in sources:
        properties = source.get("properties")
        if not isinstance(properties, dict):
            continue
        raw = properties.get("security-severity")
        if raw is None:
            continue
        try:
            return _severity_from_score(float(raw))
        except (TypeError, ValueError):
            continue
    return None


def _rule_index(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    driver = (run.get("tool") or {}).get("driver") or {}
    rules = driver.get("rules") or []
    index: dict[str, dict[str, Any]] = {}
    for rule in rules:
        if isinstance(rule, dict) and rule.get("id"):
            index[str(rule["id"])] = rule
    return index


def _location(result: dict[str, Any]) -> str:
    locations = result.get("locations") or []
    if not locations or not isinstance(locations[0], dict):
        return ""
    physical = locations[0].get("physicalLocation") or {}
    uri = str((physical.get("artifactLocation") or {}).get("uri", ""))
    line = (physical.get("region") or {}).get("startLine", "")
    if not uri:
        return ""
    return f"{uri}:{line}" if line != "" else uri


def _message(result: dict[str, Any]) -> str:
    message = result.get("message")
    if isinstance(message, dict):
        return str(message.get("text") or message.get("markdown") or "")
    return str(message or "")


def parse_sarif(document: dict[str, Any], *, scanner: str) -> list[ScanFinding]:
    """Map a SARIF document onto ScanFinding entries tagged with the gate name."""
    findings: list[ScanFinding] = []
    for run in document.get("runs") or []:
        if not isinstance(run, dict):
            continue
        rules = _rule_index(run)
        driver = (run.get("tool") or {}).get("driver") or {}
        tool_name = str(driver.get("name") or scanner)
        for result in run.get("results") or []:
            if not isinstance(result, dict):
                continue
            rule_id = str(result.get("ruleId") or result.get("rule", {}).get("id") or "")
            rule = rules.get(rule_id, {})
            severity = _security_severity(result, rule)
            if severity is None:
                severity = _LEVEL_SEVERITY.get(str(result.get("level") or "").lower(), UNKNOWN)
            text = _message(result)
            findings.append(
                ScanFinding(
                    finding_id=str(result.get("guid") or uuid.uuid4().hex),
                    scanner=scanner,
                    kind=STATIC,
                    severity=severity,
                    title=text.splitlines()[0] if text else (rule_id or tool_name),
                    detail=text,
                    location=_location(result),
                    rule_id=rule_id,
                )
            )
    return findings
