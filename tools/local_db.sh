#!/bin/bash
# A local Postgres 18 in .localdb/ (port 5434) for development and tests. Not a system service:
# it stops when the computer restarts or with `tools/local_db.sh stop`.
#
#   tools/local_db.sh start    # first run creates the cluster and the amd_dev / amd_test databases
#   tools/local_db.sh stop
#
# Needs PostgreSQL 18 (same major version as Neon): brew install postgresql@18
set -euo pipefail
export LC_ALL=C
cd "$(dirname "$0")/.."
PGBIN="${PGBIN:-$(brew --prefix postgresql@18)/bin}"
DATA=.localdb
PORT=5434

case "${1:-start}" in
  start)
    if [ ! -d "$DATA" ]; then
      "$PGBIN/initdb" -D "$DATA" -U postgres --auth=trust --encoding=UTF8 --locale=C >/dev/null
    fi
    "$PGBIN/pg_ctl" -D "$DATA" -o "-p $PORT -k /tmp" -l "$DATA/server.log" -w start
    for db in amd_dev amd_test; do
      "$PGBIN/psql" -h localhost -p "$PORT" -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
        || "$PGBIN/createdb" -h localhost -p "$PORT" -U postgres "$db"
    done
    echo "ALL_MO_DIARY__DATABASE_URL=postgresql://postgres@localhost:$PORT/amd_dev"
    echo "TEST_DATABASE_URL=postgresql://postgres@localhost:$PORT/amd_test"
    ;;
  stop)
    "$PGBIN/pg_ctl" -D "$DATA" stop
    ;;
  *)
    echo "usage: tools/local_db.sh start|stop" >&2
    exit 1
    ;;
esac
