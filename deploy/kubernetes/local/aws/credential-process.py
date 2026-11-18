"""Print the credentials inv aws-login stored, in the AWS credential_process format.

The Expiration it reports is never later than the refresh horizon, so the SDK reads the file
again within that horizon and picks up credentials that inv aws-login replaced, including
credentials that carry no expiry of their own.
"""

import json
import sys
from datetime import UTC, datetime, timedelta

CREDENTIALS = "/var/run/aws-credentials/credentials.json"
REFRESH_HORIZON = timedelta(minutes=20)

try:
    with open(CREDENTIALS, encoding="utf-8") as handle:
        record = json.load(handle)
except FileNotFoundError:
    sys.exit(f"{CREDENTIALS} is missing. Run inv aws-login on the workstation.")

horizon = datetime.now(UTC) + REFRESH_HORIZON
stored = record.get("Expiration")
expiry = datetime.fromisoformat(stored.replace("Z", "+00:00")) if stored else horizon
record["Expiration"] = min(expiry, horizon).isoformat().replace("+00:00", "Z")
print(json.dumps(record))
