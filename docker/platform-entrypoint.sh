#!/bin/sh
# Trusts the platform CA beside the system roots when the task injects PLATFORM_CA_PEM, so the
# clients verify the internal load balancer and still verify GitHub and Atlassian. httpx and
# OpenSSL read SSL_CERT_FILE, requests reads REQUESTS_CA_BUNDLE, and the gRPC OTLP exporter
# reads OTEL_EXPORTER_OTLP_CERTIFICATE. When the task injects PLATFORM_ENV_CONFIG, it also writes
# the environment's config files, which the read-only root cannot hold, and points the loader
# at them through PLATFORM_ENV_CONFIG_DIR.
set -eu

if [ -n "${PLATFORM_CA_PEM:-}" ]; then
    bundle="${TMPDIR:-/tmp}/platform-ca-bundle.pem"
    cat /etc/ssl/certs/ca-certificates.crt > "$bundle"
    printf '%s\n' "$PLATFORM_CA_PEM" >> "$bundle"
    export SSL_CERT_FILE="$bundle" REQUESTS_CA_BUNDLE="$bundle"
    export OTEL_EXPORTER_OTLP_CERTIFICATE="${OTEL_EXPORTER_OTLP_CERTIFICATE:-$bundle}"
fi

if [ -n "${PLATFORM_ENV_CONFIG:-}" ]; then
    export PLATFORM_ENV_CONFIG_DIR="${PLATFORM_ENV_CONFIG_DIR:-${TMPDIR:-/tmp}/platform-config}"
    python -m scripts.env_config_files "$PLATFORM_ENV_CONFIG_DIR"
fi

exec "$@"
