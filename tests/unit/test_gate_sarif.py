from services.orchestration.gates.sarif import parse_sarif


def _doc(results, *, rules=None, tool="semgrep"):
    driver = {"name": tool}
    if rules is not None:
        driver["rules"] = rules
    return {"version": "2.1.0", "runs": [{"tool": {"driver": driver}, "results": results}]}


def test_security_severity_maps_cvss_bands():
    doc = _doc(
        [
            {"ruleId": "r-crit", "level": "error", "message": {"text": "crit"},
             "properties": {"security-severity": "9.5"}},
            {"ruleId": "r-high", "level": "warning", "message": {"text": "high"},
             "properties": {"security-severity": "7.0"}},
            {"ruleId": "r-med", "level": "warning", "message": {"text": "med"},
             "properties": {"security-severity": "4.2"}},
            {"ruleId": "r-low", "level": "note", "message": {"text": "low"},
             "properties": {"security-severity": "1.0"}},
        ]
    )
    findings = parse_sarif(doc, scanner="semgrep")
    assert [f.severity for f in findings] == ["critical", "high", "medium", "low"]
    assert all(f.scanner == "semgrep" for f in findings)


def test_falls_back_to_level_without_security_severity():
    doc = _doc(
        [
            {"ruleId": "e", "level": "error", "message": {"text": "e"}},
            {"ruleId": "w", "level": "warning", "message": {"text": "w"}},
            {"ruleId": "n", "level": "note", "message": {"text": "n"}},
            {"ruleId": "x", "message": {"text": "x"}},
        ]
    )
    findings = parse_sarif(doc, scanner="snyk")
    assert [f.severity for f in findings] == ["high", "medium", "low", "unknown"]


def test_reads_security_severity_from_rule_definition():
    doc = _doc(
        [{"ruleId": "r1", "level": "warning", "message": {"text": "from rule"}}],
        rules=[{"id": "r1", "properties": {"security-severity": "8.8"}}],
    )
    findings = parse_sarif(doc, scanner="codeql")
    assert findings[0].severity == "high"


def test_extracts_location_and_rule_id():
    doc = _doc(
        [
            {
                "ruleId": "py/sql-injection",
                "level": "error",
                "message": {"text": "SQL injection\nmore detail"},
                "locations": [
                    {"physicalLocation": {
                        "artifactLocation": {"uri": "app/db.py"},
                        "region": {"startLine": 42},
                    }}
                ],
            }
        ]
    )
    finding = parse_sarif(doc, scanner="semgrep")[0]
    assert finding.location == "app/db.py:42"
    assert finding.rule_id == "py/sql-injection"
    assert finding.title == "SQL injection"
    assert finding.detail == "SQL injection\nmore detail"


def test_empty_and_malformed_runs_yield_no_findings():
    assert parse_sarif({}, scanner="x") == []
    assert parse_sarif({"runs": [None, {}]}, scanner="x") == []
    assert parse_sarif({"runs": [{"results": [None, "bad"]}]}, scanner="x") == []
