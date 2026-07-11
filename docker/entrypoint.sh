#!/bin/bash
set -e

# Mount JuiceFS entity filesystem if enabled
if [ "${MANOR_FS_ENABLED}" = "true" ] && command -v juicefs &>/dev/null; then
    MANOR_FS_ROOT="${MANOR_FS_ROOT:-/mnt/manor}"
    JUICEFS_META="${JUICEFS_META_URL:-redis://${REDIS_HOST:-redis}:${REDIS_PORT:-6379}/1}"

    mkdir -p "$MANOR_FS_ROOT"

    validate_juicefs_metadata() {
        if [ -z "${JUICEFS_EXPECTED_UUID:-}" ] && [ -z "${JUICEFS_EXPECTED_BUCKET:-}" ]; then
            return 0
        fi

        local status_file
        status_file="$(mktemp)"
        if ! juicefs status "$JUICEFS_META" >"$status_file" 2>/tmp/juicefs-status.err; then
            echo "[entrypoint] Failed to read JuiceFS metadata status" >&2
            cat /tmp/juicefs-status.err >&2 || true
            rm -f "$status_file"
            exit 1
        fi

        local current_uuid current_bucket
        current_uuid="$(sed -nE 's/^[[:space:]]*"UUID": "([^"]*)".*/\1/p' "$status_file" | head -n1)"
        current_bucket="$(sed -nE 's/^[[:space:]]*"Bucket": "([^"]*)".*/\1/p' "$status_file" | head -n1)"
        rm -f "$status_file"

        if [ -n "${JUICEFS_EXPECTED_UUID:-}" ] && [ "$current_uuid" != "$JUICEFS_EXPECTED_UUID" ]; then
            echo "[entrypoint] JuiceFS UUID mismatch: current=$current_uuid expected=$JUICEFS_EXPECTED_UUID" >&2
            exit 1
        fi
        if [ -n "${JUICEFS_EXPECTED_BUCKET:-}" ] && [ "$current_bucket" != "$JUICEFS_EXPECTED_BUCKET" ]; then
            echo "[entrypoint] JuiceFS bucket mismatch: current=$current_bucket expected=$JUICEFS_EXPECTED_BUCKET" >&2
            exit 1
        fi
    }

    validate_juicefs_metadata

    echo "[entrypoint] Mounting JuiceFS at $MANOR_FS_ROOT ..."
    if juicefs mount \
        "$JUICEFS_META" \
        "$MANOR_FS_ROOT" \
        --cache-dir /tmp/juicefs-cache \
        --cache-size 2048 \
        --no-usage-report \
        --background; then
        echo "[entrypoint] JuiceFS mounted at $MANOR_FS_ROOT"
    else
        echo "[entrypoint] JuiceFS mount failed" >&2
        if [ "${DEPLOYMENT_MODE:-oss}" = "cloud" ]; then
            echo "[entrypoint] Refusing to start in cloud without persistent filesystem" >&2
            exit 1
        fi
        echo "[entrypoint] Continuing without filesystem outside cloud"
    fi

    # Wait briefly for mount to settle
    sleep 1
    if mountpoint -q "$MANOR_FS_ROOT" 2>/dev/null; then
        echo "[entrypoint] JuiceFS mount confirmed."
        touch "$MANOR_FS_ROOT/.accesschk" 2>/dev/null || true
    else
        echo "[entrypoint] JuiceFS not mounted — filesystem features will use local fallback"
        if [ "${DEPLOYMENT_MODE:-oss}" = "cloud" ]; then
            echo "[entrypoint] Refusing to start in cloud without persistent filesystem" >&2
            exit 1
        fi
    fi
elif [ "${MANOR_FS_ENABLED}" = "true" ] && [ "${DEPLOYMENT_MODE:-oss}" = "cloud" ]; then
    echo "[entrypoint] JuiceFS binary not found; refusing to start in cloud with MANOR_FS_ENABLED=true" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Database bootstrap
#
# Strategy:
#   • Fresh DB (no alembic_version table):
#       In the private/cloud source tree, use SQLAlchemy create_all to build
#       all current model tables, then stamp alembic to the latest heads. The
#       OSS export strips that block and runs its generated initial Alembic
#       schema migration instead.
#
#   • Existing DB (alembic_version present):
#       Run `alembic upgrade heads` to apply any pending migrations.
# ---------------------------------------------------------------------------
if [ -f "alembic.ini" ] && [ -n "${DATABASE_URL_SYNC:-}" ]; then
    echo "[entrypoint] Waiting for database ..."
    DB_READY="false"
    for i in $(seq 1 15); do
        if python3 -c "
import sqlalchemy, sys
try:
    e = sqlalchemy.create_engine('${DATABASE_URL_SYNC}')
    e.connect().close()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            DB_READY="true"
            break
        fi
        echo "[entrypoint] DB not ready (attempt $i/15), retrying in 2s ..."
        sleep 2
    done

    if [ "$DB_READY" != "true" ]; then
        echo "[entrypoint] ERROR: Database still not reachable after 15 attempts; exiting." >&2
        exit 1
    fi


    # Detect fresh vs existing DB
    IS_FRESH=$(python3 -c "
import os, sqlalchemy as sa
e = sa.create_engine(os.environ['DATABASE_URL_SYNC'])
with e.connect() as c:
    has = sa.inspect(e).has_table('alembic_version')
    print('false' if has else 'true')
" 2>/dev/null || echo "true")

    if [ "$IS_FRESH" = "true" ]; then
        if [ -f "packages/core/migrations/versions/0001_oss_initial_schema.py" ]; then
            echo "[entrypoint] Fresh database detected — running Alembic migrations ..."
            if alembic upgrade heads 2>&1; then
                echo "[entrypoint] Alembic migrations complete."
            else
                echo "[entrypoint] ERROR: alembic upgrade heads failed." >&2
                exit 1
            fi
            echo "[entrypoint] Running data seeds ..."
            python3 scripts/init_db.py || echo "[entrypoint] WARNING: init_db seed step failed (non-fatal)"
        fi
    else
        # Remove alembic_version entries that are now ancestors of other entries.
        python3 - <<'PYEOF'
import os, sys
import sqlalchemy as sa
try:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    engine = sa.create_engine(os.environ["DATABASE_URL_SYNC"])
    with engine.begin() as conn:
        rows = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchall()
        versions = {r[0] for r in rows}
        if len(versions) > 1:
            ancestors = set()
            for v in versions:
                for other in versions - {v}:
                    try:
                        for rev in script.iterate_revisions(other, "base"):
                            if rev and rev.revision != other and rev.revision in versions:
                                ancestors.add(rev.revision)
                    except Exception:
                        pass
            for v in ancestors:
                conn.execute(sa.text("DELETE FROM alembic_version WHERE version_num = :v"), {"v": v})
            if ancestors:
                print(f"[entrypoint] Cleaned up stale alembic_version entries: {ancestors}")
except Exception as ex:
    print(f"[entrypoint] WARNING: alembic_version cleanup failed (non-fatal): {ex}", file=sys.stderr)
PYEOF

        echo "[entrypoint] Running Alembic migrations ..."
        if alembic upgrade heads 2>&1; then
            echo "[entrypoint] Alembic migrations complete."
        else
            echo "[entrypoint] ERROR: alembic upgrade heads failed." >&2
            exit 1
        fi

        # Seed MCP catalog and system tool_definitions.
        python3 scripts/init_db.py || echo "[entrypoint] WARNING: init_db seed step failed (non-fatal)"
    fi
fi

# ---------------------------------------------------------------------------
# Run the main command (uvicorn, celery, etc.) with a clean JuiceFS teardown.
#
# This process runs under tini (PID 1, see Dockerfile.api), which reaps zombies
# and forwards SIGTERM here. We deliberately do NOT `exec "$@"`: instead we run
# the app as a child and trap the stop signal so that, AFTER the app has shut
# down gracefully, we unmount JuiceFS. Leaving a stalled FUSE mount mounted when
# the container is finally SIGKILLed is what wedges the main process in
# uninterruptible D-state — un-killable by the daemon — which failed the
# 2026-06-02 deploy (see the escalate_kill self-heal in deploy.yml and the
# stop_grace_period notes in docker-compose.yml).
# ---------------------------------------------------------------------------
_unmount_juicefs() {
    if [ "${MANOR_FS_ENABLED}" = "true" ]; then
        local root="${MANOR_FS_ROOT:-/mnt/manor}"
        if mountpoint -q "$root" 2>/dev/null; then
            echo "[entrypoint] Unmounting JuiceFS at $root ..."
            # Prefer JuiceFS' own clean unmount (flushes pending writes); fall
            # back to a lazy umount so a stalled backend can never block exit.
            juicefs umount "$root" 2>/dev/null \
                || umount -l "$root" 2>/dev/null \
                || true
        fi
    fi
}

_child=0
_forward_term() {
    # Relay the stop signal to the app for graceful shutdown; the unmount runs
    # once it has actually exited (below), not here.
    [ "$_child" -ne 0 ] && kill -TERM "$_child" 2>/dev/null || true
}
trap _forward_term TERM INT

"$@" &
_child=$!

# `wait` returns early (status >128) when interrupted by the trap, so loop
# until the child has truly exited, then keep its real exit status. errexit is
# disabled here because a non-zero app exit must still run the unmount.
set +e
wait "$_child"
_status=$?
while kill -0 "$_child" 2>/dev/null; do
    wait "$_child"
    _status=$?
done
set -e

_unmount_juicefs
exit "$_status"
