#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/compose_config_smoke.sh

Validates Docker Compose configuration for the single-machine Manor shapes
without starting containers:
  - base local compose

This is intentionally a config smoke, not a Docker stack E2E test.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 127
  }
}

log() {
  printf '[compose-smoke] %s\n' "$*"
}

assert_service() {
  local services_file="$1"
  local service="$2"
  if ! grep -qx "$service" "$services_file"; then
    echo "Expected compose services to include ${service}." >&2
    cat "$services_file" >&2
    exit 1
  fi
}

assert_absent_service() {
  local services_file="$1"
  local service="$2"
  if grep -qx "$service" "$services_file"; then
    echo "Compose services must not include ${service} in the first-stage split." >&2
    cat "$services_file" >&2
    exit 1
  fi
}

check_split_services() {
  local services_file="$1"
  for service in \
    manor-api \
    manor-chat \
    manor-web \
    manor-worker \
    manor-worker-heavy \
    manor-beat \
    manor-migration \
    manor-sandbox \
    postgres \
    redis \
    minio; do
    assert_service "$services_file" "$service"
  done
  assert_absent_service "$services_file" manor-worker-embedding
  for service in redis-cache redis-celery redis-juicefs; do
    if grep -qx "$service" "$services_file"; then
      echo "K8s-style split Redis services must not appear in single-machine compose: ${service}." >&2
      cat "$services_file" >&2
      exit 1
    fi
  done
}

check_chat_worker_count() {
  local config_file="$1"
  local workers
  workers="$(
    awk '
      /^  manor-chat:$/ {
        in_chat = 1
        in_command = 0
        expect_worker_value = 0
        next
      }
      in_chat && /^  [^[:space:]][^:]*:$/ {
        in_chat = 0
        in_command = 0
        expect_worker_value = 0
      }
      in_chat && /^    command:$/ {
        in_command = 1
        next
      }
      in_chat && in_command && /^    [^[:space:]][^:]*:$/ {
        in_command = 0
        expect_worker_value = 0
      }
      in_chat && in_command {
        line = $0
        sub(/^[[:space:]]*-[[:space:]]*/, "", line)
        gsub(/^"|"$/, "", line)
        if (expect_worker_value) {
          print line
          found = 1
          exit
        }
        if (line == "--workers") {
          expect_worker_value = 1
        }
      }
      END {
        if (!found) {
          exit 1
        }
      }
    ' "$config_file"
  )" || {
    echo "manor-chat must render an explicit --workers value." >&2
    exit 1
  }
  if [[ "$workers" != "1" ]]; then
    echo "manor-chat must render with exactly one Uvicorn worker; got ${workers}." >&2
    exit 1
  fi
}

check_chat_concurrency_capacity() {
  local config_file="$1"
  local global_limit
  local per_instance_limit

  global_limit="$(compose_service_env_value "$config_file" manor-chat CHAT_STREAM_MAX_CONCURRENCY_GLOBAL)" || {
    echo "manor-chat must render CHAT_STREAM_MAX_CONCURRENCY_GLOBAL." >&2
    exit 1
  }
  per_instance_limit="$(compose_service_env_value "$config_file" manor-chat CHAT_STREAM_MAX_CONCURRENCY_PER_INSTANCE)" || {
    echo "manor-chat must render CHAT_STREAM_MAX_CONCURRENCY_PER_INSTANCE." >&2
    exit 1
  }
  if [[ "$global_limit" != "$per_instance_limit" ]]; then
    echo "single-machine compose manor-chat global concurrency must equal per-instance capacity; got global=${global_limit}, per_instance=${per_instance_limit}." >&2
    exit 1
  fi
}

compose_service_env_value() {
  local config_file="$1"
  local service="$2"
  local key="$3"
  awk -v service="$service" -v key="$key" '
    $0 == "  " service ":" {
      in_service = 1
      in_environment = 0
      next
    }
    in_service && /^  [^[:space:]][^:]*:$/ {
      in_service = 0
      in_environment = 0
    }
    in_service && /^    environment:$/ {
      in_environment = 1
      next
    }
    in_service && in_environment && /^    [^[:space:]][^:]*:$/ {
      in_environment = 0
    }
    in_service && in_environment {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      if (line ~ "^" key ":") {
        sub("^" key ":[[:space:]]*", "", line)
        gsub(/^"|"$/, "", line)
        print line
        found = 1
        exit
      }
    }
    END { exit found ? 0 : 1 }
  ' "$config_file"
}

require_compose_env_value() {
  local config_file="$1"
  local service="$2"
  local key="$3"
  local expected="$4"
  local message="$5"
  local observed

  observed="$(compose_service_env_value "$config_file" "$service" "$key")" || {
    echo "${message}; ${service}.${key} is missing." >&2
    exit 1
  }
  if [[ "$observed" != "$expected" ]]; then
    echo "${message}; expected ${service}.${key}=${expected}, got ${observed}." >&2
    exit 1
  fi
}

check_single_machine_redis_urls() {
  local config_file="$1"

  require_compose_env_value "$config_file" manor-api REDIS_URL "redis://redis:6379/0" \
    "single-machine compose must keep REDIS_URL on redis DB 0"
  require_compose_env_value "$config_file" manor-api JUICEFS_META_URL "redis://redis:6379/1" \
    "single-machine compose must keep JUICEFS_META_URL on redis DB 1"
  require_compose_env_value "$config_file" manor-api CELERY_BROKER_URL "redis://redis:6379/2" \
    "single-machine compose must keep CELERY_BROKER_URL on redis DB 2"
  require_compose_env_value "$config_file" manor-api CELERY_RESULT_BACKEND "redis://redis:6379/3" \
    "single-machine compose must keep CELERY_RESULT_BACKEND on redis DB 3"
  require_compose_env_value "$config_file" manor-worker REDIS_URL "redis://redis:6379/0" \
    "single-machine compose must keep REDIS_URL on redis DB 0"
  require_compose_env_value "$config_file" manor-worker CELERY_BROKER_URL "redis://redis:6379/2" \
    "single-machine compose must keep CELERY_BROKER_URL on redis DB 2"
  require_compose_env_value "$config_file" manor-worker CELERY_RESULT_BACKEND "redis://redis:6379/3" \
    "single-machine compose must keep CELERY_RESULT_BACKEND on redis DB 3"
  require_compose_env_value "$config_file" manor-sandbox SANDBOX_REDIS_URL "redis://redis:6379/0" \
    "single-machine compose must keep SANDBOX_REDIS_URL on redis DB 0"
}

check_sandbox_gate_defaults() {
  local config_file="$1"

  require_compose_env_value "$config_file" manor-sandbox SANDBOX_MAX_SANDBOXES "5" \
    "single-machine compose sandbox gate defaults must stay bounded"
  require_compose_env_value "$config_file" manor-sandbox SANDBOX_IDLE_TIMEOUT "600" \
    "single-machine compose sandbox gate defaults must stay bounded"
  require_compose_env_value "$config_file" manor-sandbox SANDBOX_EXEC_TIMEOUT "120" \
    "single-machine compose sandbox gate defaults must stay bounded"
  require_compose_env_value "$config_file" manor-sandbox SANDBOX_INSTANCE_MAX_EXECUTING "0" \
    "single-machine compose must use the active sandbox cap as its only capacity limit"
}

need docker

base_services="$(mktemp)"
base_config="$(mktemp)"
cleanup() {
  rm -f "$base_services" "$base_config"
}
trap cleanup EXIT

log "Rendering base single-machine compose"
CHAT_API_WORKERS="${CHAT_API_WORKERS:-1}" \
docker compose -f docker-compose.yml config --services >"$base_services"
check_split_services "$base_services"
CHAT_API_WORKERS="${CHAT_API_WORKERS:-1}" \
docker compose -f docker-compose.yml config >"$base_config"
check_chat_worker_count "$base_config"
check_chat_concurrency_capacity "$base_config"
check_single_machine_redis_urls "$base_config"
check_sandbox_gate_defaults "$base_config"


log "Compose config smoke completed"
