#!/bin/sh
# Trusts the platform CA beside the system roots when the task injects PLATFORM_CA_PEM, so the
# clients verify the internal load balancer and still verify GitHub and Atlassian. httpx and
# OpenSSL read SSL_CERT_FILE, requests reads REQUESTS_CA_BUNDLE, and the gRPC OTLP exporter
# reads OTEL_EXPORTER_OTLP_CERTIFICATE.
set -eu

if [ -n "${PLATFORM_CA_PEM:-}" ]; then
    bundle="${TMPDIR:-/tmp}/platform-ca-bundle.pem"
    cat /etc/ssl/certs/ca-certificates.crt > "$bundle"
    printf '%s\n' "$PLATFORM_CA_PEM" >> "$bundle"
    export SSL_CERT_FILE="$bundle" REQUESTS_CA_BUNDLE="$bundle"
    export OTEL_EXPORTER_OTLP_CERTIFICATE="${OTEL_EXPORTER_OTLP_CERTIFICATE:-$bundle}"
fi

exec "$@"
