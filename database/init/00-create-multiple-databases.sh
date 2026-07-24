#!/bin/sh
set -eu

create_database() {
  db="$1"
  echo "  creating database '$db'"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-EOSQL
    SELECT 'CREATE DATABASE "$db"'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db')\gexec
EOSQL
}

if [ -n "${POSTGRES_MULTIPLE_DATABASES:-}" ]; then
  echo "Multiple database creation requested: $POSTGRES_MULTIPLE_DATABASES"
  IFS=','
  for db in $POSTGRES_MULTIPLE_DATABASES; do
    create_database "$(echo "$db" | tr -d '[:space:]')"
  done
  unset IFS
  echo "Multiple databases created."
fi
