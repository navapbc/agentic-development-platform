"""Smoke test for local service health."""

from __future__ import annotations

import httpx
from database.models.schema import executions
from sqlalchemy import create_engine, literal, select

from scripts.config import get_settings, to_host_url
from scripts.db_reset import _sync_url


def _check(label: str, fn) -> bool:
    try:
        fn()
        print(f"[ok]   {label}")
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
    results.append(_check("opa", lambda: http_check(to_host_url(f"{settings.opa_url}/health"))))
    results.append(_check("langfuse", lambda: http_check(langfuse_health)))
    results.append(_check("minio object store", lambda: http_check(minio_health)))

    bedrock_ok, bedrock_detail = _bedrock_inference(settings)
    print(f"[{'ok' if bedrock_ok else 'skip'}] bedrock inference via litellm: {bedrock_detail}")

    ok = all(results)
    print("\nSMOKE TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
