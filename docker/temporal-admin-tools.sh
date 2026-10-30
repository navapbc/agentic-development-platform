#!/bin/sh
set -eu

: "${SQL_PLUGIN:=postgres12}"
: "${DBNAME:=temporal}"
: "${VISIBILITY_DBNAME:=temporal_visibility}"
: "${DEFAULT_NAMESPACE:=default}"
: "${DEFAULT_NAMESPACE_RETENTION:=24h}"
export SQL_PLUGIN

SCHEMA_ROOT=/etc/temporal/schema/postgresql/v12

apply_schema() {
    database=$1
    schema_dir=$2
    temporal-sql-tool --db "$database" setup-schema -v 0.0
    temporal-sql-tool --db "$database" update-schema -d "$SCHEMA_ROOT/$schema_dir/versioned"
}

register_namespace() {
    attempt=1
    until temporal operator cluster health >/dev/null 2>&1; do
        if [ "$attempt" -ge 30 ]; then
            echo "Temporal at ${TEMPORAL_ADDRESS} did not report healthy" >&2
            exit 1
        fi
        attempt=$((attempt + 1))
        sleep 2
    done
    if temporal operator namespace describe -n "$DEFAULT_NAMESPACE" >/dev/null 2>&1; then
        echo "Namespace ${DEFAULT_NAMESPACE} is registered"
    else
        temporal operator namespace create -n "$DEFAULT_NAMESPACE" --retention "$DEFAULT_NAMESPACE_RETENTION" --description "Default namespace for Temporal Server."
    fi
}

case "${1:-}" in
    schema)
        apply_schema "$DBNAME" temporal
        apply_schema "$VISIBILITY_DBNAME" visibility
        ;;
    namespace)
        register_namespace
        ;;
    *)
        echo "usage: $0 schema|namespace" >&2
        exit 2
        ;;
esac
