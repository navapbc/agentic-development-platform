#!/bin/sh
# Starts the Temporal server in a deployed task. ECS injects the dynamic configuration and the
# TLS material from Secrets Manager as environment values, and the server reads both from
# files, so this writes them into TEMPORAL_RUNTIME_DIR, removes the values from the
# environment, and execs the upstream entrypoint. The local stack runs the entrypoint directly.
set -eu

runtime="${TEMPORAL_RUNTIME_DIR:?TEMPORAL_RUNTIME_DIR must name a writable directory}"
umask 077
mkdir -p "$runtime"

if [ -n "${TEMPORAL_DYNAMIC_CONFIG:-}" ]; then
    printf '%s\n' "$TEMPORAL_DYNAMIC_CONFIG" > "$runtime/dynamicconfig.yaml"
    export DYNAMIC_CONFIG_FILE_PATH="$runtime/dynamicconfig.yaml"
fi
unset TEMPORAL_DYNAMIC_CONFIG

if [ -n "${TEMPORAL_TLS_INTERNODE_CERT_PEM:-}" ]; then
    printf '%s\n' "$TEMPORAL_TLS_CA_PEM" > "$runtime/ca.pem"
    printf '%s\n' "$TEMPORAL_TLS_INTERNODE_CERT_PEM" > "$runtime/internode.pem"
    printf '%s\n' "$TEMPORAL_TLS_INTERNODE_KEY_PEM" > "$runtime/internode-key.pem"
    frontend=internode
    if [ -n "${TEMPORAL_TLS_FRONTEND_CERT_PEM:-}" ]; then
        printf '%s\n' "$TEMPORAL_TLS_FRONTEND_CERT_PEM" > "$runtime/frontend.pem"
        printf '%s\n' "$TEMPORAL_TLS_FRONTEND_KEY_PEM" > "$runtime/frontend-key.pem"
        frontend=frontend
    fi
    export TEMPORAL_SERVER_CONFIG_FILE_PATH=/etc/temporal/platform-config.yaml
    export TEMPORAL_TLS_REQUIRE_CLIENT_AUTH=true
    export TEMPORAL_TLS_FRONTEND_REQUIRE_CLIENT_AUTH=false
    export TEMPORAL_TLS_SERVER_CA_CERT="$runtime/ca.pem"
    export TEMPORAL_TLS_SERVER_CERT="$runtime/internode.pem"
    export TEMPORAL_TLS_SERVER_KEY="$runtime/internode-key.pem"
    export TEMPORAL_TLS_FRONTEND_CERT="$runtime/$frontend.pem"
    export TEMPORAL_TLS_FRONTEND_KEY="$runtime/$frontend-key.pem"
fi
unset TEMPORAL_TLS_CA_PEM TEMPORAL_TLS_INTERNODE_CERT_PEM TEMPORAL_TLS_INTERNODE_KEY_PEM
unset TEMPORAL_TLS_FRONTEND_CERT_PEM TEMPORAL_TLS_FRONTEND_KEY_PEM

exec /etc/temporal/entrypoint.sh
