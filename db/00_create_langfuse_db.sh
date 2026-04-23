#!/usr/bin/env bash
# Runs inside the Postgres image on first container boot (empty pgdata volume).
# The official postgres entrypoint executes every *.sh and *.sql in
# /docker-entrypoint-initdb.d/ in alphabetical order, which is why this file
# is prefixed 00_ to run before init.sql.
#
# Creates the `langfuse` database used by the LangFuse observability service.
# Using a shell script because Postgres does not support
# `CREATE DATABASE IF NOT EXISTS` — we have to query pg_database ourselves.

set -euo pipefail

# Connect to the default `postgres` maintenance DB for the existence check —
# CREATE DATABASE cannot run inside the target DB and $PGDATABASE may be
# pointing at the primary app DB during init.
existing=$(psql --username "$POSTGRES_USER" --dbname postgres \
  --tuples-only --no-align \
  --command "SELECT 1 FROM pg_database WHERE datname = 'langfuse'")

if [[ -z "$existing" ]]; then
  psql --username "$POSTGRES_USER" --dbname postgres \
    --command "CREATE DATABASE langfuse OWNER \"$POSTGRES_USER\";"
  echo "Created database 'langfuse' (owner: $POSTGRES_USER)"
else
  echo "Database 'langfuse' already exists, skipping."
fi
