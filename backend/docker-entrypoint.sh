#!/bin/sh
# Applies database migrations when NEXUS_RUN_MIGRATIONS=true (set it on exactly one service: the API), then execs the command.
set -e
if [ "${NEXUS_RUN_MIGRATIONS:-false}" = "true" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head
fi
exec "$@"
