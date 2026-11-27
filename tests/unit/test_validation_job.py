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
    def __init__(
        self, job_states: list[dict[str, Any]], *, logs: str = REPORT, exit_code: int = 1
    ) -> None:
        self.calls: list[list[str]] = []
        self.created: list[dict[str, Any]] = []
        self._states = job_states
        self._logs = logs
        self._exit_code = exit_code

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
        elif verb[:2] == ["get", "pods"]:
            out = json.dumps({"items": [_finished_pod(self._exit_code)]})
        else:
            out = self._logs
        return process.CompletedProcess(argv, 0, out, "")


def _finished_pod(exit_code: int) -> dict[str, Any]:
    return {
        "status": {
            "containerStatuses": [
                {"name": "deployment-validate", "state": {"terminated": {"exitCode": exit_code}}}
            ]
        }
    }


def test_the_job_runs_the_cronjob_template_with_the_scope() -> None:
    job = validation_job.job_from_cronjob(CRONJOB, ["--tenant=adp", "--post"], "dv-1")

    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == [
        "python",
        "-m",
        "scripts.deployment_validate",
        "--tenant=adp",
        "--post",
    ]
    assert job["metadata"]["ownerReferences"][0]["uid"] == "u-1"


def test_a_job_with_another_entry_point_runs_that_module_from_the_template() -> None:
    job = validation_job.job_from_cronjob(
        CRONJOB, ["build-7"], "pe-1", entry="scripts.pinned_executions"
    )

    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == ["python", "-m", "scripts.pinned_executions", "build-7"]
    assert job["metadata"]["labels"] == {"app.kubernetes.io/name": "deployment-validate"}
    assert job["spec"]["backoffLimit"] == 0


def test_a_failed_job_returns_the_exit_code_of_its_container() -> None:
    kubectl = _Kubectl([{"status": {"failed": 1}}], logs="pinned", exit_code=3)

    result = validation_job.run_job(
        ["kubectl"], "ns", ["build-7"], entry="scripts.pinned_executions", run=kubectl
    )

    assert (result.passed, result.exit_code, result.report) == (False, 3, "pinned")
    assert kubectl.created[0]["metadata"]["name"].startswith("pinned-executions-")


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

    result = validation_job.run_job(
        ["kubectl", "--context", "c"], "ns", ["--all-tenants"], run=kubectl, sleep=sleeps.append
    )

    assert not result.passed and result.report == REPORT
    assert kubectl.created[0]["metadata"]["name"].startswith("deployment-validate-")
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

    result = validation_job.run_job(
        ["kubectl"],
        "ns",
        ["--platform"],
        run=kubectl,
        sleep=lambda _: None,
        clock=lambda: next(ticks),
        wait_seconds=10,
    )

    assert not result.passed and result.exit_code is None
    assert "did not finish within 10 seconds" in result.report


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
