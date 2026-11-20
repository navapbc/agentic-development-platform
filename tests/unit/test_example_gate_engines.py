"""The example gate engines follow the gate engine contract against a git checkout."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ENGINES = Path(__file__).resolve().parents[2] / "examples" / "gate-engines"
ACCESS_KEY = "AKIA" + "Q7RZ" * 4


def _load(engine: str):
    spec = importlib.util.spec_from_file_location(
        f"example_{engine.replace('-', '_')}", ENGINES / engine / "policy.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


security_policy = _load("security-policy")
test_policy = _load("test-policy")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.gov")
    _git(repo, "config", "user.name", "test")
    _write(repo, {"app/service.py": "def run():\n    return 1\n", "tests/test_service.py": ""})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _run(module, repo: Path, base: str, monkeypatch, **kwargs) -> dict:
    out = repo.parent / "out.json"
    monkeypatch.chdir(repo)
    assert module.main(["--against", base, "--json-out", str(out)], **kwargs) == 0
    return json.loads(out.read_text())


def test_security_policy_reports_only_the_lines_the_change_adds(repo, monkeypatch) -> None:
    root, base = repo
    _write(
        root,
        {
            "app/service.py": (
                "import subprocess\n"
                "def run():\n"
                "    return 1\n"
                "def deploy(cmd):\n"
                "    subprocess.run(cmd, shell=True)\n"
            ),
            "app/settings.py": f'KEY = "{ACCESS_KEY}"\nURL = "http://localhost:8000"\n',
        },
    )

    document = _run(security_policy, root, base, monkeypatch)

    results = document["runs"][0]["results"]
    located = sorted(
        (
            r["ruleId"],
            r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
            r["locations"][0]["physicalLocation"]["region"]["startLine"],
        )
        for r in results
    )
    assert document["version"] == "2.1.0"
    assert located == [
        ("aws-access-key-id", "app/settings.py", 1),
        ("python-shell-true", "app/service.py", 5),
    ]


def test_security_policy_keeps_the_matched_text_out_of_the_document(repo, monkeypatch) -> None:
    root, base = repo
    _write(root, {"config.py": f'KEY = "{ACCESS_KEY}"\n'})

    document = _run(security_policy, root, base, monkeypatch)

    assert ACCESS_KEY not in json.dumps(document)


def test_security_policy_findings_map_onto_the_platform_severity_scale(repo, monkeypatch) -> None:
    from services.orchestration.gates.sarif import parse_sarif

    root, base = repo
    _write(root, {"config.py": f'KEY = "{ACCESS_KEY}"\nrequests.get(url, verify=False)\n'})

    findings = parse_sarif(
        _run(security_policy, root, base, monkeypatch), scanner="security-policy"
    )

    assert sorted((f.rule_id, f.severity, f.location) for f in findings) == [
        ("aws-access-key-id", "critical", "config.py:1"),
        ("python-tls-verification-disabled", "high", "config.py:2"),
    ]


def test_security_policy_refuses_an_unreadable_rules_file(repo, monkeypatch, tmp_path) -> None:
    root, base = repo
    monkeypatch.chdir(root)
    bad = tmp_path / "rules.toml"
    bad.write_text('[[rule]]\nid = "x"\ndescription = "x"\npattern = "x"\nseverity = "urgent"\n')

    code = security_policy.main(
        ["--against", base, "--json-out", str(tmp_path / "o.json"), "--rules", str(bad)]
    )

    assert code == 2


def test_test_policy_flags_source_changed_without_a_test(repo, monkeypatch) -> None:
    root, base = repo
    _write(
        root,
        {
            "app/service.py": "def run():\n    return 2\n",
            "app/billing.py": "def charge():\n    return 3\n",
            "tests/test_service.py": "def test_run():\n    assert True\n",
        },
    )

    document = _run(test_policy, root, base, monkeypatch, env={})

    assert [(c["path"], c["line"], c["severity"]) for c in document["comments"]] == [
        ("app/billing.py", 1, "MEDIUM")
    ]
    assert document["comments"][0]["detail"] == test_policy.FIXED_DETAIL


def test_test_policy_approves_a_change_with_its_tests(repo, monkeypatch) -> None:
    root, base = repo
    _write(root, {"app/service.py": "def run():\n    return 2\n", "tests/test_service.py": "x\n"})

    document = _run(test_policy, root, base, monkeypatch, env={})

    assert document["comments"] == []
    assert document["review_action"] == "APPROVE"


class _Gateway(BaseHTTPRequestHandler):
    reply: bytes = b""
    status = 200
    received: list[tuple[dict, dict]] = []

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).received.append(({k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(self.status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(self.reply)

    def log_message(self, *args) -> None:
        return None


@pytest.fixture
def gateway():
    _Gateway.received = []
    server = HTTPServer(("127.0.0.1", 0), _Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield _Gateway, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _gateway_env(url: str) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": url,
        "ANTHROPIC_API_KEY": "gateway-key",
        "AI_REVIEW_MODEL": "review-model",
        "ANTHROPIC_CUSTOM_HEADERS": (
            "langfuse_session_id: exec-1\nlangfuse_generation_name: test-policy"
        ),
    }


def test_test_policy_adds_the_gateway_suggestions_to_the_detail(repo, monkeypatch, gateway) -> None:
    handler, url = gateway
    suggestions = {"app/billing.py": ["charge returns 3", "charge is idempotent"]}
    text = f"Here you go:\n{json.dumps(suggestions)}"
    handler.status = 200
    handler.reply = json.dumps({"content": [{"type": "text", "text": text}]}).encode()
    root, base = repo
    _write(root, {"app/billing.py": "def charge():\n    return 3\n"})

    document = _run(test_policy, root, base, monkeypatch, env=_gateway_env(url))

    headers, body = handler.received[0]
    assert headers["langfuse_session_id"] == "exec-1"
    assert headers["x-api-key"] == "gateway-key"
    assert headers["authorization"] == "Bearer " + "gateway-key"
    assert body["model"] == "review-model"
    assert body["temperature"] == 0
    assert "app/billing.py" in body["messages"][0]["content"]
    detail = document["comments"][0]["detail"]
    assert detail.startswith(test_policy.FIXED_DETAIL)
    assert "- charge returns 3\n- charge is idempotent" in detail


def test_test_policy_retries_without_temperature_for_a_model_that_refuses_it(
    repo, monkeypatch, gateway
) -> None:
    handler, url = gateway
    replies = iter(
        [
            (400, b'{"error": {"message": "`temperature` is deprecated for this model."}}'),
            (
                200,
                json.dumps(
                    {"content": [{"type": "text", "text": '{"app/billing.py": ["x"]}'}]}
                ).encode(),
            ),
        ]
    )

    def reply(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        handler.received.append(({}, body))
        status, payload = next(replies)
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    monkeypatch.setattr(handler, "do_POST", reply)
    root, base = repo
    _write(root, {"app/billing.py": "def charge():\n    return 3\n"})

    document = _run(test_policy, root, base, monkeypatch, env=_gateway_env(url))

    assert [("temperature" in body) for _, body in handler.received] == [True, False]
    assert document["comments"][0]["detail"].endswith("Suggested test cases:\n- x")


def test_test_policy_keeps_its_findings_when_the_gateway_fails(repo, monkeypatch, gateway) -> None:
    handler, url = gateway
    handler.status = 503
    handler.reply = b"{}"
    root, base = repo
    _write(root, {"app/billing.py": "def charge():\n    return 3\n"})

    document = _run(test_policy, root, base, monkeypatch, env=_gateway_env(url))

    assert [c["path"] for c in document["comments"]] == ["app/billing.py"]
    assert document["comments"][0]["detail"] == test_policy.FIXED_DETAIL


def test_test_policy_makes_no_model_call_without_findings(repo, monkeypatch, gateway) -> None:
    handler, url = gateway
    root, base = repo
    _write(root, {"app/service.py": "def run():\n    return 2\n", "tests/test_service.py": "x\n"})

    _run(test_policy, root, base, monkeypatch, env=_gateway_env(url))

    assert handler.received == []


def test_test_policy_findings_map_onto_the_platform_findings(repo, monkeypatch) -> None:
    from services.orchestration.gates.gate_engine import map_review_findings

    root, base = repo
    _write(root, {"app/billing.py": "def charge():\n    return 3\n"})

    findings = map_review_findings(
        _run(test_policy, root, base, monkeypatch, env={}), scanner="test-policy"
    )

    assert [(f.severity, f.location, f.rule_id) for f in findings] == [
        ("medium", "app/billing.py:1", "test-coverage")
    ]


@pytest.mark.parametrize(
    ("source", "test", "covered"),
    [
        ("src/api/handler.ts", "src/api/handler.test.ts", True),
        ("pkg/store/store.go", "pkg/store/store_test.go", True),
        ("svc/src/main/java/Billing.java", "svc/src/test/java/BillingTest.java", True),
        ("app/billing.py", "tests/test_service.py", False),
    ],
)
def test_test_policy_maps_sources_to_their_tests(source, test, covered) -> None:
    mappings = test_policy.load_rules(ENGINES / "test-policy" / "rules.toml")

    found = test_policy.uncovered(mappings, {source: 1, test: 1})

    assert (found == []) is covered
