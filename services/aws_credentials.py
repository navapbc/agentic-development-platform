"""Report how long the credentials inv aws-login stored remain valid."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RENEW = "inv aws-login"

log = logging.getLogger(__name__)


def expiry_of(record: Mapping[str, Any]) -> datetime | None:
    stored = record.get("Expiration")
    return datetime.fromisoformat(str(stored).replace("Z", "+00:00")) if stored else None


def expiry_report(record: Mapping[str, Any], warn_seconds: int, now: datetime) -> tuple[bool, str]:
    """Return whether the credentials need renewal and a sentence that says when they expire."""
    expiry = expiry_of(record)
    if expiry is None:
        return False, "the credentials carry no expiry"
    stamp = expiry.astimezone(UTC).isoformat().replace("+00:00", "Z")
    remaining = (expiry - now).total_seconds()
    if remaining <= 0:
        return True, f"the credentials expired at {stamp}. Run {RENEW}"
    if remaining <= warn_seconds:
        minutes = int(remaining // 60)
        return True, f"the credentials expire at {stamp}, in {minutes} minutes. Run {RENEW}"
    return False, f"the credentials expire at {stamp}"


async def watch_credentials(
    path: Path,
    *,
    warn_seconds: int,
    interval: float = 300.0,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Log a warning naming the renewal command while the stored credentials are near expiry."""
    while True:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.warning("%s is missing. Run %s", path, RENEW)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("%s is unreadable: %s", path, exc)
        else:
            renew, report = expiry_report(record, warn_seconds, now())
            if renew:
                log.warning("%s", report)
        await asyncio.sleep(interval)
