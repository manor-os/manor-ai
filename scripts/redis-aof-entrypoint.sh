#!/bin/sh
set -eu

DATA_DIR="${REDIS_DATA_DIR:-/data}"
RDB_PATH="$DATA_DIR/dump.rdb"
AOF_DIR="$DATA_DIR/appendonlydir"
SOCKET_PATH="/tmp/manor-redis-aof-migrate-$$.sock"
PID_PATH="/tmp/manor-redis-aof-migrate-$$.pid"

if [ "$(id -u)" = "0" ]; then
  chown -R redis:redis "$DATA_DIR"
  if command -v gosu >/dev/null 2>&1; then
    exec gosu redis:redis /bin/sh "$0" "$@"
  fi
  if command -v su-exec >/dev/null 2>&1; then
    exec su-exec redis:redis /bin/sh "$0" "$@"
  fi
  if command -v su >/dev/null 2>&1; then
    exec su -s /bin/sh redis "$0" "$@"
  fi
  echo "ERROR Cannot drop Redis root privileges: gosu, su-exec, and su are unavailable." >&2
  exit 1
fi

cleanup_migration_server() {
  if [ -S "$SOCKET_PATH" ]; then
    redis-cli -s "$SOCKET_PATH" SHUTDOWN NOSAVE >/dev/null 2>&1 || true
  fi
  rm -f "$SOCKET_PATH" "$PID_PATH"
}

has_aof_payload() {
  [ -d "$AOF_DIR" ] && [ -n "$(find "$AOF_DIR" -type f -size +0c -print -quit 2>/dev/null)" ]
}

if [ -s "$RDB_PATH" ] && ! has_aof_payload; then
  echo "INFO Migrating existing Redis RDB to AOF before startup."
  trap cleanup_migration_server EXIT INT TERM
  redis-server \
    --dir "$DATA_DIR" \
    --dbfilename "$(basename "$RDB_PATH")" \
    --appendonly no \
    --port 0 \
    --unixsocket "$SOCKET_PATH" \
    --unixsocketperm 700 \
    --daemonize yes \
    --pidfile "$PID_PATH"

  ready=0
  for _ in $(seq 1 100); do
    if [ "$(redis-cli -s "$SOCKET_PATH" PING 2>/dev/null || true)" = "PONG" ]; then
      ready=1
      break
    fi
    sleep 0.1
  done
  if [ "$ready" != "1" ]; then
    echo "ERROR Redis RDB migration server did not become ready." >&2
    exit 1
  fi

  redis-cli -s "$SOCKET_PATH" CONFIG SET appendfsync everysec >/dev/null
  redis-cli -s "$SOCKET_PATH" CONFIG SET appendonly yes >/dev/null

  migrated=0
  for _ in $(seq 1 600); do
    persistence="$(redis-cli -s "$SOCKET_PATH" INFO persistence 2>/dev/null || true)"
    in_progress="$(printf '%s\n' "$persistence" | sed -n 's/^aof_rewrite_in_progress:\([0-9][0-9]*\).*/\1/p')"
    scheduled="$(printf '%s\n' "$persistence" | sed -n 's/^aof_rewrite_scheduled:\([0-9][0-9]*\).*/\1/p')"
    rewrite_status="$(printf '%s\n' "$persistence" | sed -n 's/^aof_last_bgrewrite_status:\([^[:space:]]*\).*/\1/p')"
    if [ "${in_progress:-1}" = "0" ] && [ "${scheduled:-1}" = "0" ] && [ "$rewrite_status" = "ok" ] && has_aof_payload; then
      migrated=1
      break
    fi
    sleep 0.1
  done
  if [ "$migrated" != "1" ]; then
    echo "ERROR Redis RDB to AOF migration did not complete; refusing to start with an empty AOF." >&2
    exit 1
  fi

  cleanup_migration_server
  trap - EXIT INT TERM
  echo "OK Redis RDB migrated to AOF."
fi

exec redis-server \
  --dir "$DATA_DIR" \
  --appendonly yes \
  --appendfsync everysec \
  --maxmemory "${REDIS_MAXMEMORY:-0}" \
  --maxmemory-policy "${REDIS_MAXMEMORY_POLICY:-noeviction}"
