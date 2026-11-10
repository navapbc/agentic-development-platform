#!/bin/sh
set -e

if [ -f /kube/config ]; then
    mkdir -p /home/appuser/.kube
    python scripts/rewrite_kube_config.py </kube/config >/home/appuser/.kube/config
    chmod 600 /home/appuser/.kube/config
fi

exec python -m services.orchestration.worker
