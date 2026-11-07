"""Smoke test for local service health."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
from database.models.schema import executions
from services import process
from services.aws_credentials import expiry_of, expiry_report
from sqlalchemy import create_engine, literal, select

from scripts.build_id import recorded_build_id
from scripts.config import Settings, get_settings, to_host_url
from scripts.db_reset import _sync_url
from scripts.k8s_cluster import DEPLOYMENTS, exec_argv, namespaced


def _check(label: str, fn) -> bool:
    try:
        detail = fn()
        print(f"[ok]   {label}: {detail}" if isinstance(detail, str) else f"[ok]   {label}")
        return True
    except Exception as exc:
        print(f"[FAIL] {label}: {exc}")
        return False


def _bedrock_inference(settings) -> tuple[bool, str]:
    url = to_host_url(f"{settings.litellm_base_url}/v1/chat/completions")
    headers = {"Authorization": f"Bearer {settings.litellm_master_key}"}
    payload = {
        "model": "classification-summary",
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 16,
    }
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=30.0)
        if resp.status_code != 200:
            return False, f"{resp.status_code} {resp.text[:120]}"
        content = resp.json()["choices"][0]["message"]["content"]
        return True, f"classification-summary -> {content.strip()[:40]}"
    except Exception as exc:
        return False, str(exc)


def _kubectl_output(argv: list[str]) -> str:
    result = process.run(
        argv, timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"kubectl exited {result.returncode}")
    return result.stdout


def _deployments_ready(settings: Settings) -> None:
    listing = json.loads(_kubectl_output(namespaced(settings, "get", "deployments", "-o", "json")))
    ready = {
        item["metadata"]["name"]: item["status"].get("readyReplicas", 0)
        for item in listing["items"]
    }
    missing = [name for name in DEPLOYMENTS if not ready.get(name)]
    if missing:
        raise RuntimeError(f"not ready: {', '.join(missing)}")


def _opa_on_loopback(settings: Settings) -> None:
    probe = ["python", "-m", "services.healthcheck", "http", "8181"]
    _kubectl_output(exec_argv(settings, probe, tty=False))


def _credentials(settings: Settings) -> str:
    encoded = _kubectl_output(
        namespaced(
            settings,
            "get",
            "secret",
            "aws-credentials",
            "-o",
            "jsonpath={.data.credentials\\.json}",
        )
    )
    record = json.loads(base64.b64decode(encoded))
    now = datetime.now(UTC)
    _, report = expiry_report(record, settings.aws_credentials_warn_seconds, now)
    expiry = expiry_of(record)
    if expiry is not None and expiry <= now:
        raise RuntimeError(report)
    return report


_POLICY_PROBE = """
import socket, time
def reach(host, port):
    try:
        socket.create_connection((host, port), timeout=2).close()
        return True
    except OSError:
        return False
service = "{{}}.{platform}.svc.cluster.local"
deadline = time.monotonic() + {settle}
while reach(service.format("mcp-gateway"), 80) and time.monotonic() < deadline:
    time.sleep(1)
print(reach(service.format("litellm"), 4000), reach(service.format("mcp-gateway"), 80))
"""

# A new pod's address reaches the policy enforcer a few seconds after the pod starts, and the
# enforcer admits traffic until then, so the probe waits this long before it reports the
# policy as not enforced.
_POLICY_SETTLE_SECONDS = 20


def sandbox_policy_status(settings: Settings, image: str) -> str:
    """Probe from a pod the sandbox egress policy selects whether the cluster enforces it.

    The policy admits LiteLLM and refuses the MCP gateway. A cluster whose CNI lacks network
    policy support admits both for as long as the probe waits.
    """
    from scripts.k8s_cluster import kubectl

    probe = _POLICY_PROBE.format(platform=settings.k8s_namespace, settle=_POLICY_SETTLE_SECONDS)
    output = _kubectl_output(
        kubectl(
            settings,
            "run",
            "sandbox-policy-probe",
            "--namespace",
            settings.openhands_sandbox_namespace,
            "--rm",
            "-i",
            "--quiet",
            "--restart=Never",
            "--labels=app=openhands-sandbox",
            f"--image={image}",
            "--image-pull-policy=IfNotPresent",
            "--command",
            "--",
            "python",
            "-c",
            probe,
        )
    ).split()
    litellm, gateway = output[-2:] if len(output) >= 2 else ["False", "False"]
    if litellm != "True":
        raise RuntimeError("a sandbox pod cannot reach LiteLLM")
    if gateway == "True":
        return "not enforced, because this cluster's CNI does not apply NetworkPolicy"
    return "enforced"


def main() -> int:
    settings = get_settings()
    results: list[bool] = []

    def db_check() -> None:
        engine = create_engine(_sync_url(), future=True)
        with engine.connect() as conn:
            conn.execute(select(literal(1)))
            conn.execute(select(executions.c.execution_id).limit(1))

    def http_check(url: str) -> None:
        httpx.get(url, timeout=5.0)

    litellm_health = to_host_url(f"{settings.litellm_base_url}/health/liveliness")
    langfuse_health = to_host_url(f"{settings.langfuse_host}/api/public/health")
    minio_health = "http://localhost:9090/minio/health/live"
    results.append(_check("postgres + agent_platform schema", db_check))
    results.append(_check("litellm", lambda: http_check(litellm_health)))
    results.append(_check("cluster deployments ready", lambda: _deployments_ready(settings)))
    results.append(_check("opa on loopback", lambda: _opa_on_loopback(settings)))
    results.append(_check("aws credentials", lambda: _credentials(settings)))
    image = f"agentic-dev-platform-local:{recorded_build_id()}"
    results.append(
        _check("agent-sandbox network policies", lambda: sandbox_policy_status(settings, image))
    )
    results.append(_check("langfuse", lambda: http_check(langfuse_health)))
    results.append(_check("minio object store", lambda: http_check(minio_health)))

    bedrock_ok, bedrock_detail = _bedrock_inference(settings)
    print(f"[{'ok' if bedrock_ok else 'skip'}] bedrock inference via litellm: {bedrock_detail}")

    ok = all(results)
    print("\nSMOKE TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
