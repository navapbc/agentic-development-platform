#!/bin/sh
set -e

mkdir -p /home/appuser/.kube
sed 's#https://127.0.0.1:6443#https://kubernetes.docker.internal:6443#' /kube/config > /home/appuser/.kube/config

exec python -m services.orchestration.worker
