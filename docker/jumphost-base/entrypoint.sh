#!/bin/sh
# Keeps the Fargate jumphost task alive so ECS Exec can attach and forward a port.
# The task self-terminates after DURATION_IN_SECS so a forgotten session stops billing.
set -eu

name="${NAME:-jumphost}"
interval="${INTERVAL:-30}"
duration="${DURATION_IN_SECS:-3600}"
elapsed=0

echo "$(date '+%F %T') ${name} up for ${duration}s at ${interval}s intervals"

while [ "${elapsed}" -lt "${duration}" ]; do
  sleep "${interval}"
  elapsed=$((elapsed + interval))
done

echo "$(date '+%F %T') ${name} reached ${duration}s TTL, exiting"
