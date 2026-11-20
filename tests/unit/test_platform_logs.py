"""Reading the platform log records the local collector stores as OTLP JSON lines."""

from __future__ import annotations

import json

from scripts.platform_logs import records


def _line(body: str, service: str = "workflow-worker.activity") -> str:
    return json.dumps(
        {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                    },
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "timeUnixNano": "1794626160000000000",
                                    "severityText": "INFO",
                                    "body": {"stringValue": body},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    )


def test_each_record_yields_its_time_service_severity_and_body() -> None:
    lines = [_line("event='rebar_poll_new' ticket_id='0316'"), "", _line("other")]

    assert list(records(lines)) == [
        (
            "2026-11-14T03:16:00.000+00:00",
            "workflow-worker.activity",
            "INFO",
            "event='rebar_poll_new' ticket_id='0316'",
        ),
        ("2026-11-14T03:16:00.000+00:00", "workflow-worker.activity", "INFO", "other"),
    ]
