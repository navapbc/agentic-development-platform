#!/bin/sh
# Starts the Temporal UI in a deployed task. ECS injects the CA bundle that issued the frontend
# certificate as TEMPORAL_TLS_CA_PEM, and the UI reads it from a file. The load balancer in
# front of the UI does not verify the certificate it is served, so the UI presents a
# certificate issued at start, and no key leaves the task.
set -eu

runtime="${TEMPORAL_UI_RUNTIME_DIR:?TEMPORAL_UI_RUNTIME_DIR must name a writable directory}"
umask 077
mkdir -p "$runtime"

if [ -n "${TEMPORAL_TLS_CA_PEM:-}" ]; then
    printf '%s\n' "$TEMPORAL_TLS_CA_PEM" > "$runtime/ca.pem"
    export TEMPORAL_TLS_CA="$runtime/ca.pem"
fi
unset TEMPORAL_TLS_CA_PEM

openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 397 \
    -subj "/CN=temporal-ui" -keyout "$runtime/ui-key.pem" -out "$runtime/ui.pem" 2>/dev/null
export TEMPORAL_UI_SERVER_TLS_CERT="$runtime/ui.pem"
export TEMPORAL_UI_SERVER_TLS_KEY="$runtime/ui-key.pem"

cd /home/ui-server
exec ./ui-server --env docker start
