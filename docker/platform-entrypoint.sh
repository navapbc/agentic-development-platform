#!/bin/sh
# Trusts the platform CA beside the system roots when the task injects PLATFORM_CA_PEM, so the
# clients verify the internal load balancer and still verify GitHub and Atlassian. httpx and
# OpenSSL read SSL_CERT_FILE, requests reads REQUESTS_CA_BUNDLE, and the gRPC OTLP exporter
# reads OTEL_EXPORTER_OTLP_CERTIFICATE. When the task injects PLATFORM_ENV_CONFIG, it also writes
# the environment's config files, which the read-only root cannot hold, and points the loader
# at them through PLATFORM_ENV_CONFIG_DIR. When the pod sets PLATFORM_SECRETS_DIR, it exports
# each file the Secrets Store CSI driver mounts there as the variable the file name names, so no
# Kubernetes Secret holds the values.
set -eu

if [ -n "${PLATFORM_SECRETS_DIR:-}" ]; then
    for file in "$PLATFORM_SECRETS_DIR"/*; do
        [ -f "$file" ] || continue
        name="${file##*/}"
        case "$name" in
            [!A-Za-z_]* | *[!A-Za-z0-9_]*)
                echo "platform-entrypoint: ignoring $name, which is not a variable name" >&2
                continue
                ;;
        esac
        value="$(cat "$file")"
        export "$name=$value"
    done
fi

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
