#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$APP_DB_PASSWORD" --set=gateway_password="$GATEWAY_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE sp_app LOGIN PASSWORD %L', :'app_password') \gexec
SELECT format('CREATE ROLE sp_gateway LOGIN PASSWORD %L', :'gateway_password') \gexec
CREATE DATABASE supportpilot OWNER sp_app;
CREATE DATABASE litellm OWNER sp_gateway;
SQL
