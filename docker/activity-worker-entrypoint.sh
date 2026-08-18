#!/bin/sh
set -e

if [ -f /kube/config ]; then
    mkdir -p /home/appuser/.kube
    python - <<'PY'
import re

import yaml

with open("/kube/config") as handle:
    cfg = yaml.safe_load(handle)

for entry in cfg.get("clusters", []):
    cluster = entry.get("cluster", {})
    cluster["server"] = re.sub(
        r"https://[^:/]+", "https://host.docker.internal", cluster.get("server", "")
    )
    cluster["insecure-skip-tls-verify"] = True
    cluster.pop("certificate-authority-data", None)
    cluster.pop("certificate-authority", None)

with open("/home/appuser/.kube/config", "w") as handle:
    yaml.safe_dump(cfg, handle)
PY
    chmod 600 /home/appuser/.kube/config
fi

exec python -m services.orchestration.worker
