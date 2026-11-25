from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts import validation_job
from services import process

CRONJOB = {
    "metadata": {"name": "deployment-validate", "namespace": "agentic-dev-platform", "uid": "u-1"},
    "spec": {
        "jobTemplate": {
            "metadata": {"labels": {"app.kubernetes.io/name": "deployment-validate"}},
            "spec": {
                "backoffLimit": 0,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "deployment-validate",
                                "args": ["python", "-m", "scripts.deployment_validate", "--x"],
                            }
                        ]
                    }
                },
            },
        }
    },
}

REPORT = "\n".join(
    [
        "Deployment validation of the platform",
        "Result: 0 failed, 2 passed, 0 skipped",
        "PASS Database schema",
        "",
        "Deployment validation of tenant adp",
        "Result: 1 failed, 1 passed, 0 skipped",
        "FAIL Slack auth",
        "  Cause: Slack refused auth.test with invalid_auth",
        "  Fix: Set the bot token.",
        "PASS Tenant bundle",
        "  Detail: bundle 1 is active",
    ]
)


class _Kubectl:
    def __init__(self, job_states: list[dict[str, Any]], *, logs: str = REPORT) -> None:
        self.calls: list[list[str]] = []
        self.created: list[dict[str, Any]] = []
        self._states = job_states
        self._logs = logs

    def __call__(self, argv, *, input=None, **_) -> process.CompletedProcess:
        self.calls.append(list(argv))
        verb = argv[argv.index("--namespace") + 2 :]
        if verb[:2] == ["get", "cronjob"]:
            out = json.dumps(CRONJOB)
        elif verb[0] == "create":
            self.created.append(json.loads(input))
            out = ""
        elif verb[:2] == ["get", "job"]:
            out = json.dumps(self._states.pop(0))
        else:
            out = self._logs
        return process.CompletedProcess(argv, 0, out, "")


def test_the_job_runs_the_cronjob_template_with_the_scope() -> None:
    job = validation_job.job_from_cronjob(CRONJOB, ["--tenant=adp", "--post"], "dv-1")

    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == [*validation_job.ENTRY, "--tenant=adp", "--post"]
    assert job["metadata"]["ownerReferences"][0]["uid"] == "u-1"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"platform": True}, ["--platform"]),
        ({"all_tenants": True}, ["--all-tenants"]),
        ({"tenant": "adp"}, ["--tenant=adp"]),
    ],
)
def test_exactly_one_scope_is_accepted(kwargs, expected) -> None:
    assert validation_job.scope_args(**kwargs) == expected


@pytest.mark.parametrize("kwargs", [{}, {"platform": True, "tenant": "adp"}])
def test_no_scope_or_two_scopes_are_refused(kwargs) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        validation_job.scope_args(**kwargs)


def test_a_failed_job_returns_its_report_after_waiting() -> None:
    kubectl = _Kubectl([{"status": {}}, {"status": {"failed": 1}}])
    sleeps: list[float] = []

    ok, report = validation_job.run_job(
        ["kubectl", "--context", "c"], "ns", ["--all-tenants"], run=kubectl, sleep=sleeps.append
    )

    assert not ok and report == REPORT
    assert kubectl.created[0]["spec"]["template"]["spec"]["containers"][0]["args"][-1] == (
        "--all-tenants"
    )
    assert sleeps == [validation_job.POLL_SECONDS]
    assert all(call[:3] == ["kubectl", "--context", "c"] for call in kubectl.calls)


def test_the_poll_outlasts_the_job_deadline() -> None:
    manifest = Path(__file__).resolve().parents[2] / "deploy/kubernetes/deployment-validate.yaml"
    cronjob = next(
        document
        for document in yaml.safe_load_all(manifest.read_text(encoding="utf-8"))
        if document["kind"] == "CronJob"
    )

    job_deadline = cronjob["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"]

    assert job_deadline < validation_job.WAIT_SECONDS


def test_a_job_that_never_finishes_fails_at_the_deadline() -> None:
    kubectl = _Kubectl([{"status": {}}] * 3)
    ticks = iter([0.0, 5.0, 11.0])

    ok, report = validation_job.run_job(
        ["kubectl"],
        "ns",
        ["--platform"],
        run=kubectl,
        sleep=lambda _: None,
        clock=lambda: next(ticks),
        wait_seconds=10,
    )

    assert not ok and "did not finish within 10 seconds" in report


def test_a_rollout_prints_only_the_failures_and_does_not_fail(capsys) -> None:
    kubectl = _Kubectl([{"status": {"failed": 1}}])

    ok = validation_job.report_after_rollout(["kubectl"], run=kubectl)

    out = capsys.readouterr().out
    assert not ok
    assert "FAIL Slack auth" in out and "invalid_auth" in out
    assert "PASS" not in out and "bundle 1 is active" not in out


def test_a_rollout_whose_validation_cannot_start_continues(capsys) -> None:
    def refuse(argv, **_) -> process.CompletedProcess:
        return process.CompletedProcess(argv, 1, "", 'cronjobs "deployment-validate" not found')

    assert validation_job.report_after_rollout(["kubectl"], run=refuse) is False
    assert "the validation did not run" in capsys.readouterr().out
