#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

NAMESPACE="${NAMESPACE:-manor-local}"
EXPECTED_CONTEXT="${EXPECTED_CONTEXT:-orbstack}"
SOURCE_POSTGRES_CONTAINER="${SOURCE_POSTGRES_CONTAINER:-manor-os-postgres}"
SOURCE_REDIS_CONTAINER="${SOURCE_REDIS_CONTAINER:-manor-os-redis}"
SOURCE_MINIO_CONTAINER="${SOURCE_MINIO_CONTAINER:-manor-os-minio}"
SOURCE_API_CONTAINER="${SOURCE_API_CONTAINER:-manor-os-api}"
MIGRATION_ROOT="${MANOR_LOCAL_MIGRATION_ROOT:-${TMPDIR:-/tmp}}"
TARGET_MINIO_PORT="${TARGET_MINIO_PORT:-19020}"
TARGET_FS_POD="manor-compose-local-migration-fs"
TARGET_APP_DEPLOYMENTS=(
  manor-api
  manor-chat
  manor-web
  manor-worker
  manor-worker-heavy
  manor-beat
  manor-sandbox
)

confirmation=""
migration_dir=""
source_fs_container=""
source_fs_started=false
source_filesystem_kib=""
target_fs_pod_created=false
target_quiesced=false
minio_port_forward_pid=""
success=false
source_started_containers=()
source_stopped_apps=()

usage() {
  cat <<'EOF'
Usage:
  scripts/compose_to_local_k8s.sh --confirm migrate-compose-to-manor-local

Copies the local Docker Compose PostgreSQL database, application MinIO bucket,
and logical JuiceFS /mnt/manor files into the local orbstack/manor-local K8s
environment. The target data is backed up into a temporary migration directory
before replacement. Redis, JuiceFS metadata, and historical recovery buckets
are intentionally not migrated.

Environment:
  EXPECTED_CONTEXT                Kubernetes context, must be orbstack.
  NAMESPACE                       Kubernetes namespace, must be manor-local.
  SOURCE_POSTGRES_CONTAINER       Source Compose PostgreSQL container.
  SOURCE_REDIS_CONTAINER          Source Compose Redis container.
  SOURCE_MINIO_CONTAINER          Source Compose MinIO container.
  SOURCE_API_CONTAINER            Source Compose API container used to mount JuiceFS.
  MANOR_LOCAL_MIGRATION_ROOT      Parent directory for temporary migration state.
  TARGET_MINIO_PORT               Local port used for temporary K8s MinIO forwarding.
EOF
}

log() {
  printf '[compose-to-local-k8s] %s\n' "$*"
}

die() {
  printf '[compose-to-local-k8s] ERROR: %s\n' "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

container_env() {
  local container="$1"
  local name="$2"

  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
    | sed -n "s/^${name}=//p" \
    | head -n 1
}

file_env() {
  local env_file="$1"
  local name="$2"

  sed -n "s/^${name}=//p" "$env_file" | head -n 1
}

container_is_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$1")" == "true" ]]
}

start_source_container_if_needed() {
  local container="$1"

  if container_is_running "$container"; then
    return
  fi
  log "Starting source container ${container}"
  docker start "$container" >/dev/null
  source_started_containers+=("$container")
}

stop_source_apps() {
  local container

  for container in "$SOURCE_API_CONTAINER" manor-os-worker manor-os-worker-work manor-os-web manor-os-sandbox; do
    if ! docker inspect "$container" >/dev/null 2>&1; then
      continue
    fi
    if container_is_running "$container"; then
      log "Stopping source application container ${container} for a consistent snapshot"
      docker stop --time 30 "$container" >/dev/null
      source_stopped_apps+=("$container")
    fi
  done
}

wait_for_source_dependencies() {
  local attempt

  for ((attempt = 1; attempt <= 30; attempt++)); do
    if docker exec "$SOURCE_POSTGRES_CONTAINER" sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1 \
      && docker exec "$SOURCE_REDIS_CONTAINER" redis-cli ping 2>/dev/null | grep -qx PONG \
      && docker exec "$SOURCE_MINIO_CONTAINER" curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1; then
      return
    fi
    sleep 2
  done
  die "Timed out waiting for source PostgreSQL, Redis, and MinIO"
}

wait_for_target_port_forward() {
  local attempt

  for ((attempt = 1; attempt <= 20; attempt++)); do
    if curl -fsS "http://127.0.0.1:${TARGET_MINIO_PORT}/minio/health/live" >/dev/null 2>&1; then
      return
    fi
    if ! kill -0 "$minio_port_forward_pid" 2>/dev/null; then
      cat "$migration_dir/target-minio-port-forward.log" >&2 || true
      die "Target MinIO port-forward exited before becoming ready"
    fi
    sleep 1
  done
  cat "$migration_dir/target-minio-port-forward.log" >&2 || true
  die "Timed out waiting for target MinIO port-forward"
}

start_target_minio_port_forward() {
  if curl -fsS --max-time 1 "http://127.0.0.1:${TARGET_MINIO_PORT}/minio/health/live" >/dev/null 2>&1; then
    die "TARGET_MINIO_PORT ${TARGET_MINIO_PORT} is already in use"
  fi

  kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" \
    port-forward --address 127.0.0.1 service/minio "${TARGET_MINIO_PORT}:9000" \
    >"$migration_dir/target-minio-port-forward.log" 2>&1 < /dev/null &
  minio_port_forward_pid="$!"
  wait_for_target_port_forward
}

ensure_minio_client() {
  if docker image inspect minio/mc >/dev/null 2>&1; then
    return
  fi
  log "Pulling MinIO client image"
  docker pull minio/mc >/dev/null
}

create_target_fs_pod() {
  kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" delete pod "$TARGET_FS_POD" --ignore-not-found=true >/dev/null
  cat <<EOF | kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${TARGET_FS_POD}
  labels:
    app.kubernetes.io/name: ${TARGET_FS_POD}
spec:
  restartPolicy: Never
  containers:
    - name: transfer
      image: busybox:1.36
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts:
        - name: manor-fs
          mountPath: /mnt/manor
  volumes:
    - name: manor-fs
      persistentVolumeClaim:
        claimName: manor-fs
EOF
  target_fs_pod_created=true
  kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" wait --for=condition=Ready "pod/${TARGET_FS_POD}" --timeout=180s
}

ensure_target_filesystem_capacity() {
  local target_available_kib
  local required_kib

  target_available_kib="$(kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec "$TARGET_FS_POD" -- \
    sh -c 'df -Pk /mnt/manor | awk "NR == 2 { print \$4 }"')"
  [[ "$source_filesystem_kib" =~ ^[0-9]+$ && "$target_available_kib" =~ ^[0-9]+$ ]] \
    || die "Could not determine source or target filesystem capacity"
  required_kib=$((source_filesystem_kib + source_filesystem_kib / 10))
  if (( target_available_kib < required_kib )); then
    die "Filesystem requires ${required_kib} KiB including 10% headroom, but target PVC has ${target_available_kib} KiB available"
  fi
  log "Filesystem capacity check passed: source=${source_filesystem_kib} KiB target_available=${target_available_kib} KiB"
}

ensure_target_persistent_storage() {
  local deployment
  local claim
  local mount_path
  local claims
  local mounts

  for deployment in postgres minio; do
    if [[ "$deployment" == "postgres" ]]; then
      claim="postgres-data"
      mount_path="/var/lib/postgresql/data"
    else
      claim="minio-data"
      mount_path="/data"
    fi

    kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" get "pvc/${claim}" >/dev/null \
      || die "Target ${deployment} persistent volume claim ${claim} is missing"
    claims="$(kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" get "deployment/${deployment}" -o jsonpath='{range .spec.template.spec.volumes[*]}{.persistentVolumeClaim.claimName}{"\n"}{end}')"
    mounts="$(kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" get "deployment/${deployment}" -o jsonpath='{range .spec.template.spec.containers[*].volumeMounts[*]}{.mountPath}{"\n"}{end}')"
    grep -Fx "$claim" <<<"$claims" >/dev/null \
      || die "Target ${deployment} does not mount persistent volume claim ${claim}"
    grep -Fx "$mount_path" <<<"$mounts" >/dev/null \
      || die "Target ${deployment} does not mount persistent storage at ${mount_path}"
  done
}

quiesce_target_apps() {
  local deployment

  log "Stopping target application deployments"
  for deployment in "${TARGET_APP_DEPLOYMENTS[@]}"; do
    kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" scale "deployment/${deployment}" --replicas=0 >/dev/null
  done
  for deployment in "${TARGET_APP_DEPLOYMENTS[@]}"; do
    kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" rollout status "deployment/${deployment}" --timeout=180s
  done
  target_quiesced=true
}

backup_target_data() {
  local target_minio_env="$migration_dir/target-minio.env"
  local target_minio_user
  local target_minio_password
  local target_minio_bucket

  log "Backing up target PostgreSQL and /mnt/manor"
  kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec deployment/postgres -- \
    sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
    >"$migration_dir/target-postgres.dump"
  kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec "$TARGET_FS_POD" -- \
    tar -C /mnt/manor -cf - . >"$migration_dir/target-manor-fs.tar"

  kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec deployment/minio -- printenv >"$target_minio_env"
  target_minio_user="$(file_env "$target_minio_env" MINIO_ROOT_USER)"
  target_minio_password="$(file_env "$target_minio_env" MINIO_ROOT_PASSWORD)"
  target_minio_bucket="$(kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" get configmap manor-runtime-config -o jsonpath='{.data.MINIO_BUCKET}')"
  [[ -n "$target_minio_user" && -n "$target_minio_password" && -n "$target_minio_bucket" ]] \
    || die "Target MinIO configuration is incomplete"

  TARGET_MINIO_USER="$target_minio_user" \
  TARGET_MINIO_PASSWORD="$target_minio_password" \
  TARGET_MINIO_BUCKET="$target_minio_bucket" \
  TARGET_MINIO_PORT="$TARGET_MINIO_PORT" \
  docker run --rm -v "$migration_dir:/migration" \
    -e TARGET_MINIO_USER -e TARGET_MINIO_PASSWORD -e TARGET_MINIO_BUCKET -e TARGET_MINIO_PORT \
    --entrypoint /bin/sh minio/mc -ec '
      mc alias set target "http://host.docker.internal:${TARGET_MINIO_PORT}" "$TARGET_MINIO_USER" "$TARGET_MINIO_PASSWORD"
      if mc stat "target/${TARGET_MINIO_BUCKET}" >/dev/null 2>&1; then
        mc mirror "target/${TARGET_MINIO_BUCKET}" /migration/target-minio
      fi
    '
}

start_source_filesystem_mount() {
  local source_api_env_file="$migration_dir/source-api.env"
  local source_api_image
  local source_network
  local attempt

  source_api_image="$(docker inspect -f '{{.Config.Image}}' "$SOURCE_API_CONTAINER")"
  source_network="$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$SOURCE_API_CONTAINER")"
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$SOURCE_API_CONTAINER" >"$source_api_env_file"

  source_fs_container="manor-compose-to-local-k8s-fs-$$"
  log "Mounting source JuiceFS in temporary container"
  docker run -d --name "$source_fs_container" --privileged --device /dev/fuse \
    --network "$source_network" --env-file "$source_api_env_file" \
    --entrypoint /bin/bash "$source_api_image" -c '
      set -euo pipefail
      : "${JUICEFS_META_URL:?JUICEFS_META_URL is required}"
      mkdir -p /mnt/manor
      juicefs mount "$JUICEFS_META_URL" /mnt/manor \
        --cache-dir /tmp/juicefs-cache \
        --cache-size 2048 \
        --no-usage-report \
        --background
      for attempt in $(seq 1 30); do
        mountpoint -q /mnt/manor && break
        sleep 1
      done
      mountpoint -q /mnt/manor
      cleanup() {
        juicefs umount /mnt/manor 2>/dev/null || umount -l /mnt/manor 2>/dev/null || true
      }
      trap cleanup EXIT TERM INT
      while true; do
        sleep 3600 &
        wait "$!"
      done
    ' >/dev/null
  source_fs_started=true

  for ((attempt = 1; attempt <= 30; attempt++)); do
    if docker exec "$source_fs_container" mountpoint -q /mnt/manor; then
      return
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "$source_fs_container")" != "true" ]]; then
      docker logs "$source_fs_container" >&2 || true
      die "Source JuiceFS mount container exited before becoming ready"
    fi
    sleep 2
  done
  docker logs "$source_fs_container" >&2 || true
  die "Timed out waiting for the source JuiceFS mount"
}

stage_source_filesystem() {
  local staging_available_kib
  local staging_required_kib
  local archive="$migration_dir/source-manor-fs.tar"

  source_filesystem_kib="$(docker exec "$source_fs_container" sh -c 'du -sk /mnt/manor | awk "NR == 1 { print \$1 }"')"
  staging_available_kib="$(df -Pk "$MIGRATION_ROOT" | awk 'NR == 2 { print $4 }')"
  [[ "$source_filesystem_kib" =~ ^[0-9]+$ && "$staging_available_kib" =~ ^[0-9]+$ ]] \
    || die "Could not determine source filesystem or migration staging capacity"
  staging_required_kib=$((source_filesystem_kib + source_filesystem_kib / 2 + 1024 * 1024))
  if (( staging_available_kib < staging_required_kib )); then
    die "Migration staging requires ${staging_required_kib} KiB, but ${MIGRATION_ROOT} has ${staging_available_kib} KiB available"
  fi

  log "Creating source logical filesystem snapshot (${source_filesystem_kib} KiB)"
  docker exec "$source_fs_container" tar -C /mnt/manor -cf - . >"$archive"
  [[ -s "$archive" ]] || die "Source logical filesystem snapshot is empty"
  log "Source logical filesystem snapshot created at ${archive}"
}

restore_database() {
  log "Restoring source PostgreSQL into target PostgreSQL"
  docker exec "$SOURCE_POSTGRES_CONTAINER" \
    sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges' \
    | kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec -i deployment/postgres -- \
      sh -c 'pg_restore --exit-on-error --clean --if-exists --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}

restore_minio() {
  local source_minio_user
  local source_minio_password
  local source_minio_bucket
  local source_minio_port
  local target_minio_env="$migration_dir/target-minio.env"
  local target_minio_user
  local target_minio_password
  local target_minio_bucket

  source_minio_user="$(container_env "$SOURCE_MINIO_CONTAINER" MINIO_ROOT_USER)"
  source_minio_password="$(container_env "$SOURCE_MINIO_CONTAINER" MINIO_ROOT_PASSWORD)"
  source_minio_bucket="$(container_env "$SOURCE_API_CONTAINER" MINIO_BUCKET)"
  source_minio_port="$(docker port "$SOURCE_MINIO_CONTAINER" 9000/tcp | head -n 1 | sed 's/.*://')"
  target_minio_user="$(file_env "$target_minio_env" MINIO_ROOT_USER)"
  target_minio_password="$(file_env "$target_minio_env" MINIO_ROOT_PASSWORD)"
  target_minio_bucket="$(kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" get configmap manor-runtime-config -o jsonpath='{.data.MINIO_BUCKET}')"
  [[ -n "$source_minio_user" && -n "$source_minio_password" && -n "$source_minio_bucket" && -n "$source_minio_port" ]] \
    || die "Source MinIO configuration is incomplete"

  log "Mirroring source MinIO bucket ${source_minio_bucket} into target bucket ${target_minio_bucket}"
  SOURCE_MINIO_USER="$source_minio_user" \
  SOURCE_MINIO_PASSWORD="$source_minio_password" \
  SOURCE_MINIO_BUCKET="$source_minio_bucket" \
  SOURCE_MINIO_PORT="$source_minio_port" \
  TARGET_MINIO_USER="$target_minio_user" \
  TARGET_MINIO_PASSWORD="$target_minio_password" \
  TARGET_MINIO_BUCKET="$target_minio_bucket" \
  TARGET_MINIO_PORT="$TARGET_MINIO_PORT" \
  docker run --rm -v "$migration_dir:/migration" \
    -e SOURCE_MINIO_USER -e SOURCE_MINIO_PASSWORD -e SOURCE_MINIO_BUCKET -e SOURCE_MINIO_PORT \
    -e TARGET_MINIO_USER -e TARGET_MINIO_PASSWORD -e TARGET_MINIO_BUCKET -e TARGET_MINIO_PORT \
    --entrypoint /bin/sh minio/mc -ec '
      mc alias set source "http://host.docker.internal:${SOURCE_MINIO_PORT}" "$SOURCE_MINIO_USER" "$SOURCE_MINIO_PASSWORD"
      mc alias set target "http://host.docker.internal:${TARGET_MINIO_PORT}" "$TARGET_MINIO_USER" "$TARGET_MINIO_PASSWORD"
      mc mb --ignore-existing "target/${TARGET_MINIO_BUCKET}"
      mc mirror --overwrite --remove "source/${SOURCE_MINIO_BUCKET}" "target/${TARGET_MINIO_BUCKET}"
      mc diff --quiet "source/${SOURCE_MINIO_BUCKET}" "target/${TARGET_MINIO_BUCKET}" > /migration/minio-diff.txt
      test ! -s /migration/minio-diff.txt
    '
}

restore_filesystem() {
  local source_entries
  local target_entries
  local archive="$migration_dir/source-manor-fs.tar"

  log "Copying source logical /mnt/manor into target PVC"
  kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec "$TARGET_FS_POD" -- \
    sh -c 'rm -rf /mnt/manor/* /mnt/manor/.[!.]* /mnt/manor/..?*'
  kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec -i "$TARGET_FS_POD" -- \
    tar -C /mnt/manor -xpf - <"$archive"

  source_entries="$(tar -tf "$archive" | wc -l | tr -d ' ')"
  target_entries="$(kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec "$TARGET_FS_POD" -- tar -C /mnt/manor -cf - . | tar -tf - | wc -l | tr -d ' ')"
  [[ "$source_entries" == "$target_entries" ]] \
    || die "Filesystem entry count mismatch: source=${source_entries} target=${target_entries}"
}

verify_database() {
  local source_tables
  local target_tables

  source_tables="$(docker exec "$SOURCE_POSTGRES_CONTAINER" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema = '\''public'\''"')"
  target_tables="$(kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec deployment/postgres -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema = '\''public'\''"')"
  [[ "$source_tables" == "$target_tables" ]] \
    || die "Database table count mismatch: source=${source_tables} target=${target_tables}"
}

cleanup() {
  local exit_status=$?
  local index

  if [[ -n "$minio_port_forward_pid" ]]; then
    kill "$minio_port_forward_pid" >/dev/null 2>&1 || true
    wait "$minio_port_forward_pid" >/dev/null 2>&1 || true
  fi
  if [[ "$target_fs_pod_created" == "true" ]]; then
    kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" delete pod "$TARGET_FS_POD" --ignore-not-found=true >/dev/null 2>&1 || true
  fi
  if [[ "$source_fs_started" == "true" ]]; then
    docker logs "$source_fs_container" >"$migration_dir/source-filesystem.log" 2>&1 || true
    docker rm -f "$source_fs_container" >/dev/null 2>&1 || true
  fi
  for ((index = ${#source_started_containers[@]} - 1; index >= 0; index--)); do
    docker stop "${source_started_containers[$index]}" >/dev/null 2>&1 || true
  done
  for ((index = 0; index < ${#source_stopped_apps[@]}; index++)); do
    docker start "${source_stopped_apps[$index]}" >/dev/null 2>&1 || true
  done

  if [[ "$success" == "true" && -n "$migration_dir" ]]; then
    rm -rf "$migration_dir"
    log "Migration staging directory cleared"
  elif [[ -n "$migration_dir" ]]; then
    log "Migration failed; staging directory preserved at ${migration_dir}"
    if [[ "$target_quiesced" == "true" ]]; then
      log "Target applications remain scaled to zero; inspect the staging directory before recovery"
    fi
  fi
  exit "$exit_status"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm)
      [[ $# -ge 2 ]] || die "--confirm requires a value"
      confirmation="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "Unknown argument: $1"
      ;;
  esac
done

[[ "$confirmation" == "migrate-compose-to-manor-local" ]] \
  || die "Use --confirm migrate-compose-to-manor-local to replace local K8s data"

need docker
need kubectl
need curl
need tar

current_context="$(kubectl config current-context)"
if [[ "$current_context" != "$EXPECTED_CONTEXT" || "$current_context" != "orbstack" ]]; then
  die "Refusing to migrate into context '${current_context}'; expected local orbstack"
fi
if [[ "$NAMESPACE" != "manor-local" ]]; then
  die "Refusing to migrate into namespace '${NAMESPACE}'; expected manor-local"
fi

for container in "$SOURCE_POSTGRES_CONTAINER" "$SOURCE_REDIS_CONTAINER" "$SOURCE_MINIO_CONTAINER" "$SOURCE_API_CONTAINER"; do
  docker inspect "$container" >/dev/null 2>&1 || die "Source container does not exist: ${container}"
done
for deployment in postgres redis minio "${TARGET_APP_DEPLOYMENTS[@]}"; do
  kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" get "deployment/${deployment}" >/dev/null
done
kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" get pvc/manor-fs >/dev/null
ensure_target_persistent_storage

mkdir -p "$MIGRATION_ROOT"
migration_dir="$(mktemp -d "$MIGRATION_ROOT/manor-compose-to-local-k8s.XXXXXX")"
chmod 0700 "$migration_dir"
trap cleanup EXIT HUP INT TERM
exec > >(tee -a "$migration_dir/migration.log") 2>&1

stop_source_apps
start_source_container_if_needed "$SOURCE_POSTGRES_CONTAINER"
start_source_container_if_needed "$SOURCE_REDIS_CONTAINER"
start_source_container_if_needed "$SOURCE_MINIO_CONTAINER"
wait_for_source_dependencies
ensure_minio_client
start_source_filesystem_mount
stage_source_filesystem
start_target_minio_port_forward

quiesce_target_apps
create_target_fs_pod
ensure_target_filesystem_capacity
backup_target_data
restore_database
restore_minio
restore_filesystem

log "Clearing target Redis cache and Celery queues"
kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec deployment/redis -- redis-cli FLUSHALL >/dev/null
verify_database

log "Reapplying local K8s stack to run migrations and restore workloads"
EXPECTED_CONTEXT="$EXPECTED_CONTEXT" NAMESPACE="$NAMESPACE" BUILD_IMAGES=false \
  scripts/k8s_local_apply.sh
kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" exec deployment/manor-api -- \
  curl -fsS http://127.0.0.1:8000/health >/dev/null
verify_database

success=true
log "Compose data migration completed"
