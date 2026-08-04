#!/bin/sh
set -e

mkdir -p /home/appuser/.kube
sed 's#https://127.0.0.1:6443#https://kubernetes.docker.internal:6443#' /kube/config > /home/appuser/.kube/config

if [ -n "$JIRA_URL" ] && [ -n "$JIRA_API_TOKEN" ] && [ -n "$JIRA_EMAIL" ]; then
  ACLI_SITE=$(printf '%s' "$JIRA_URL" | sed -E 's#^https?://##; s#/+$##')
  printf '%s' "$JIRA_API_TOKEN" | "${JIRA_ACLI_PATH:-acli}" jira auth login --site "$ACLI_SITE" --email "$JIRA_EMAIL" --token
fi

exec python -m services.orchestration.worker
