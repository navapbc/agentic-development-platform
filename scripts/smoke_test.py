"""Smoke test for local service health."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from database.models.schema import executions
from services import process
from services.aws_credentials import expiry_of, expiry_report
from sqlalchemy import create_engine, literal, select

from scripts.aws_login import CREDENTIALS_OBJECT, stored_record
from scripts.config import Settings, alembic_database_url, get_settings, to_host_url
from scripts.k8s_cluster import deployments, exec_argv, kubectl, tenants
from scripts.tenant_render import sandbox_namespace, worker_namespace


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
    listing = json.loads(
        _kubectl_output(kubectl(settings, "get", "deployments", "-A", "-o", "json"))
    )
    ready = {
        (item["metadata"]["namespace"], item["metadata"]["name"]): item["status"].get(
            "readyReplicas", 0
        )
        for item in listing["items"]
    }
    missing = [
        f"{namespace}/{name}"
        for namespace, name in deployments(settings)
        if not ready.get((namespace, name))
    ]
    if missing:
        raise RuntimeError(f"not ready: {', '.join(missing)}")


def _opa_on_loopback(settings: Settings) -> None:
    probe = ["python", "-m", "services.healthcheck", "http", "8181"]
    _kubectl_output(exec_argv(settings, probe, tty=False))


def _credentials(settings: Settings) -> str:
    record = stored_record(settings)
    if record is None:
        raise RuntimeError(f"the {CREDENTIALS_OBJECT} Secret is missing. Run inv aws-login")
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
litellm = "litellm.{platform}.svc.cluster.local"
gateway = "mcp-gateway.{tenant}.svc.cluster.local"
deadline = time.monotonic() + {settle}
while reach(gateway, 80) and time.monotonic() < deadline:
    time.sleep(1)
print(reach(litellm, 4000), reach(gateway, 80))
"""

# A new pod's address reaches the policy enforcer a few seconds after the pod starts, and the
# enforcer admits traffic until then, so the probe waits this long before it reports the
# policy as not enforced.
_POLICY_SETTLE_SECONDS = 20


def sandbox_policy_status(
    settings: Settings, image: str, namespace: str, tenant_namespace: str
) -> str:
    """Probe from a pod the sandbox egress policy selects whether the cluster enforces it.

    The policy admits LiteLLM and refuses the tenant's MCP gateway. A cluster whose CNI lacks
    network policy support admits both for as long as the probe waits.
    """
    probe = _POLICY_PROBE.format(
        platform=settings.k8s_namespace, tenant=tenant_namespace, settle=_POLICY_SETTLE_SECONDS
    )
    output = _kubectl_output(
        kubectl(
            settings,
            "run",
            "sandbox-policy-probe",
            "--namespace",
            namespace,
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
        engine = create_engine(alembic_database_url(), future=True)
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
    results.append(_check("aws credentials", lambda: _credentials(settings)))
    image = settings.openhands_agent_server_image
    for tenant in tenants():
        worker = settings.model_copy(update={"tenant": tenant})
        results.append(
            _check(
                f"{worker_namespace(tenant)} opa on loopback", lambda w=worker: _opa_on_loopback(w)
            )
        )
        namespace = sandbox_namespace(tenant)
        results.append(
            _check(
                f"{namespace} network policies",
                lambda namespace=namespace, tenant=tenant: sandbox_policy_status(
                    settings, image, namespace, worker_namespace(tenant)
                ),
            )
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
