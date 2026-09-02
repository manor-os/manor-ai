#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/generate_nango_runtime_env.sh test|prod OUTPUT_FILE [--compare OTHER_ENV_FILE]

Generates a new environment-specific Nango bundle without printing secret
values. OUTPUT_FILE must not already exist.
EOF
}

die() {
  echo "$*" >&2
  exit 2
}

env_file_value() {
  local env_file="$1"
  local key="$2"
  awk -v key="$key" '
    index($0, "=") {
      current_key = substr($0, 1, index($0, "=") - 1)
      if (current_key == key) {
        print substr($0, index($0, "=") + 1)
        found = 1
        exit
      }
    }
    END { exit found ? 0 : 1 }
  ' "$env_file"
}

validate_bundle_shape() {
  local env_file="$1"
  local label="$2"
  local expected actual

  expected="$(printf '%s\n' \
    NANGO_DB_PASSWORD \
    NANGO_ENCRYPTION_KEY \
    NANGO_SECRET_KEY \
    NANGO_PUBLIC_KEY \
    NANGO_CONNECT_HMAC_KEY \
    NANGO_ADMIN_INVITE_TOKEN)"
  actual="$(awk '
    /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
    index($0, "=") != 0 {
      key = substr($0, 1, index($0, "=") - 1)
      if (key !~ /^[A-Za-z_][A-Za-z0-9_]*$/ || seen[key]++) exit 2
      print key
      next
    }
    { exit 2 }
  ' "$env_file")" || die "${label} has invalid or duplicate keys"
  [[ "$actual" == "$expected" ]] || die "${label} must contain exactly the six ordered Nango runtime keys"
}

validate_bundle_values() {
  local env_file="$1"
  local label="$2"
  local db_password encryption_key secret_key public_key connect_hmac_key invite_token

  validate_bundle_shape "$env_file" "$label"
  db_password="$(env_file_value "$env_file" NANGO_DB_PASSWORD)"
  encryption_key="$(env_file_value "$env_file" NANGO_ENCRYPTION_KEY)"
  secret_key="$(env_file_value "$env_file" NANGO_SECRET_KEY)"
  public_key="$(env_file_value "$env_file" NANGO_PUBLIC_KEY)"
  connect_hmac_key="$(env_file_value "$env_file" NANGO_CONNECT_HMAC_KEY)"
  invite_token="$(env_file_value "$env_file" NANGO_ADMIN_INVITE_TOKEN)"

  [[ "$db_password" =~ ^[0-9a-f]{64}$ ]] || die "${label} NANGO_DB_PASSWORD must be 64 lowercase hexadecimal characters"
  [[ "$secret_key" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] || die "${label} NANGO_SECRET_KEY must be a UUID v4"
  [[ "$public_key" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] || die "${label} NANGO_PUBLIC_KEY must be a UUID v4"
  [[ "$connect_hmac_key" =~ ^[0-9a-f]{64}$ ]] || die "${label} NANGO_CONNECT_HMAC_KEY must be 64 lowercase hexadecimal characters"
  [[ "$invite_token" =~ ^[0-9a-f]{64}$ ]] || die "${label} NANGO_ADMIN_INVITE_TOKEN must be 64 lowercase hexadecimal characters"
  [[ "$encryption_key" =~ ^[A-Za-z0-9+/]{43}=$ ]] || die "${label} NANGO_ENCRYPTION_KEY must be a base64-encoded 32-byte value"

  [[ "$db_password" != "nango_secret" ]] || die "${label} contains the unsafe NANGO_DB_PASSWORD default"
  [[ "$invite_token" != "manor-dev-admin" ]] || die "${label} contains the unsafe NANGO_ADMIN_INVITE_TOKEN default"
  [[ "$encryption_key" != "RzV0YjJ4d3F1cFdfQXFqSHdSeUF6UWp4bUtxN1Y4WkE=" ]] || die "${label} contains the unsafe NANGO_ENCRYPTION_KEY default"

  if [[ "$db_password" == "$encryption_key" || "$db_password" == "$secret_key" \
    || "$db_password" == "$public_key" || "$db_password" == "$connect_hmac_key" \
    || "$db_password" == "$invite_token" \
    || "$encryption_key" == "$secret_key" || "$encryption_key" == "$public_key" \
    || "$encryption_key" == "$connect_hmac_key" || "$encryption_key" == "$invite_token" \
    || "$secret_key" == "$public_key" || "$secret_key" == "$connect_hmac_key" \
    || "$secret_key" == "$invite_token" || "$public_key" == "$connect_hmac_key" \
    || "$public_key" == "$invite_token" || "$connect_hmac_key" == "$invite_token" ]]; then
    die "${label} must use a distinct value for every Nango runtime key"
  fi
}

fingerprint() {
  printf '%s' "$1" | openssl dgst -sha256 | awk '{print $NF}'
}

if [[ $# -ne 2 && $# -ne 4 ]]; then
  usage >&2
  exit 2
fi

environment="$1"
output_file="$2"
compare_file=""
case "$environment" in
  test|prod) ;;
  *) die "Environment must be test or prod" ;;
esac
if [[ $# -eq 4 ]]; then
  [[ "$3" == "--compare" ]] || die "Expected --compare before the counterpart bundle path"
  compare_file="$4"
fi

[[ ! -e "$output_file" ]] || die "Output file already exists: ${output_file}"
[[ -z "$compare_file" || -f "$compare_file" ]] || die "Counterpart bundle does not exist: ${compare_file}"

command -v openssl >/dev/null 2>&1 || die "openssl is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
umask 077
output_dir="$(dirname "$output_file")"
[[ -d "$output_dir" ]] || die "Output directory does not exist: ${output_dir}"
temporary_file="$(mktemp "${output_file}.tmp.XXXXXX")"
cleanup() { rm -f "$temporary_file"; }
trap cleanup EXIT

NANGO_DB_PASSWORD="$(openssl rand -hex 32)"
NANGO_ENCRYPTION_KEY="$(openssl rand -base64 32 | tr -d '\n')"
NANGO_SECRET_KEY="$(python3 -c 'import uuid; print(uuid.uuid4())')"
NANGO_PUBLIC_KEY="$(python3 -c 'import uuid; print(uuid.uuid4())')"
NANGO_CONNECT_HMAC_KEY="$(openssl rand -hex 32)"
NANGO_ADMIN_INVITE_TOKEN="$(openssl rand -hex 32)"

printf '%s\n' \
  "NANGO_DB_PASSWORD=${NANGO_DB_PASSWORD}" \
  "NANGO_ENCRYPTION_KEY=${NANGO_ENCRYPTION_KEY}" \
  "NANGO_SECRET_KEY=${NANGO_SECRET_KEY}" \
  "NANGO_PUBLIC_KEY=${NANGO_PUBLIC_KEY}" \
  "NANGO_CONNECT_HMAC_KEY=${NANGO_CONNECT_HMAC_KEY}" \
  "NANGO_ADMIN_INVITE_TOKEN=${NANGO_ADMIN_INVITE_TOKEN}" \
  >"$temporary_file"
validate_bundle_values "$temporary_file" "generated bundle"

if [[ -n "$compare_file" ]]; then
  validate_bundle_values "$compare_file" "counterpart bundle"
  for key in NANGO_DB_PASSWORD NANGO_ENCRYPTION_KEY NANGO_SECRET_KEY NANGO_PUBLIC_KEY NANGO_CONNECT_HMAC_KEY NANGO_ADMIN_INVITE_TOKEN; do
    generated_value="$(env_file_value "$temporary_file" "$key")"
    counterpart_value="$(env_file_value "$compare_file" "$key")"
    [[ "$generated_value" != "$counterpart_value" ]] || die "${key} must differ between test and prod"
  done
fi

chmod 600 "$temporary_file"
mv "$temporary_file" "$output_file"
trap - EXIT

printf 'Generated %s Nango bundle: %s\n' "$environment" "$output_file"
for key in NANGO_DB_PASSWORD NANGO_ENCRYPTION_KEY NANGO_SECRET_KEY NANGO_PUBLIC_KEY NANGO_CONNECT_HMAC_KEY NANGO_ADMIN_INVITE_TOKEN; do
  value="$(env_file_value "$output_file" "$key")"
  printf '%s_SHA256=%s\n' "$key" "$(fingerprint "$value")"
done
