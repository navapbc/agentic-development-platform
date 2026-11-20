#!/bin/sh
set -eu

create_database() {
  db="$1"
  echo "  creating database '$db'"
  psql -v ON_ERROR_STOP=1 -v db="$db" --username "$POSTGRES_USER" --dbname postgres <<-'EOSQL'
    SELECT format('CREATE DATABASE %I', :'db')
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db')\gexec
EOSQL
}

if [ -n "${POSTGRES_MULTIPLE_DATABASES:-}" ]; then
  echo "Multiple database creation requested: $POSTGRES_MULTIPLE_DATABASES"
  printf '%s\n' "$POSTGRES_MULTIPLE_DATABASES" | tr ',' '\n' | while read -r db; do
    db="$(printf '%s' "$db" | tr -d '[:space:]')"
    if [ -n "$db" ]; then
      create_database "$db"
    fi
  done
  echo "Multiple databases created."
fi
