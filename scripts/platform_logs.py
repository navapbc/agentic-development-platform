"""Print the platform log records the local collector stored, optionally filtered by text.

The collector's logs pipeline writes every exported record as OTLP JSON lines to the
otel-logs volume, rotating the file. The collector image has no shell, so the files are
read through a one-off container that mounts the volume read-only.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

from services import process

LOG_DIR = "/var/lib/otelcol/logs"
READER_IMAGE = (
    "public.ecr.aws/amazonlinux/amazonlinux:2023-minimal"
    "@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4"
)


def records(lines: Iterable[str]) -> Iterator[tuple[str, str, str, str]]:
    """Yield (time, service, severity, body) for each log record in OTLP JSON lines."""
    for line in lines:
        if not line.strip():
            continue
        batch = json.loads(line)
        for resource_logs in batch.get("resourceLogs", []):
            attributes = resource_logs.get("resource", {}).get("attributes", [])
            service = next(
                (
                    attribute["value"].get("stringValue", "")
                    for attribute in attributes
                    if attribute.get("key") == "service.name"
                ),
                "",
            )
            for scope_logs in resource_logs.get("scopeLogs", []):
                for record in scope_logs.get("logRecords", []):
                    nanos = int(
                        record.get("timeUnixNano") or record.get("observedTimeUnixNano") or 0
                    )
                    moment = datetime.fromtimestamp(nanos / 1e9, UTC).isoformat(
                        timespec="milliseconds"
                    )
                    body = record.get("body", {}).get("stringValue", "")
                    yield moment, service, record.get("severityText", ""), body


def stored_lines(volume: str) -> list[str]:
    output = process.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:{LOG_DIR}:ro",
            READER_IMAGE,
            "sh",
            "-c",
            f"cat $(ls -tr {LOG_DIR}/platform-logs*.jsonl 2>/dev/null) 2>/dev/null || true",
        ],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return output.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match", default="", help="Print only records whose body holds this.")
    parser.add_argument(
        "--volume", default="agentic-dev-platform_otel-logs", help="The collector's log volume."
    )
    args = parser.parse_args(argv)
    for moment, service, severity, body in records(stored_lines(args.volume)):
        if args.match in body:
            print(f"{moment} {service} {severity} {body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
