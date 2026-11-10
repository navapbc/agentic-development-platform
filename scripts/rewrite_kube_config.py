import re
import sys

import yaml

cfg = yaml.safe_load(sys.stdin)

if cfg is None:
    raise RuntimeError("/kube/config is empty")

for entry in cfg.get("clusters", []):
    cluster = entry.get("cluster", {})
    cluster["server"] = re.sub(
        r"https://[^:/]+", "https://host.docker.internal", cluster.get("server", "")
    )
    cluster["insecure-skip-tls-verify"] = True
    cluster.pop("certificate-authority-data", None)
    cluster.pop("certificate-authority", None)

yaml.safe_dump(cfg, sys.stdout)
