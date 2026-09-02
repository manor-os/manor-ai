#!/usr/bin/env bash

manor_trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

manor_ipv4_to_int() {
  local ip="$1"
  local first second third fourth extra octet
  IFS=. read -r first second third fourth extra <<<"$ip"
  [[ -n "$first" && -n "$second" && -n "$third" && -n "$fourth" && -z "${extra:-}" ]] || return 1
  for octet in "$first" "$second" "$third" "$fourth"; do
    [[ "$octet" =~ ^[0-9]+$ ]] || return 1
    [[ "${#octet}" -eq 1 || "$octet" != 0* ]] || return 1
    (( 10#$octet <= 255 )) || return 1
  done
  printf '%u\n' "$((
    (10#$first << 24) |
    (10#$second << 16) |
    (10#$third << 8) |
    10#$fourth
  ))"
}

manor_validate_private_ipv4_cidr() {
  local cidr="$1"
  local address prefix address_int mask first second third fourth
  [[ "$cidr" == */* ]] || return 1
  address="${cidr%/*}"
  prefix="${cidr##*/}"
  [[ "$prefix" =~ ^[0-9]+$ ]] || return 1
  prefix=$((10#$prefix))
  (( prefix >= 16 && prefix <= 32 )) || return 1
  address_int="$(manor_ipv4_to_int "$address")" || return 1
  mask=$(( (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF ))
  (( (address_int & mask) == address_int )) || return 1
  IFS=. read -r first second third fourth <<<"$address"
  if (( 10#$first == 10 )); then
    return 0
  fi
  if (( 10#$first == 172 && 10#$second >= 16 && 10#$second <= 31 )); then
    return 0
  fi
  if (( 10#$first == 192 && 10#$second == 168 )); then
    return 0
  fi
  return 1
}

manor_ipv4_in_cidr() {
  local ip="$1"
  local cidr="$2"
  local address prefix ip_int address_int mask
  address="${cidr%/*}"
  prefix=$((10#${cidr##*/}))
  ip_int="$(manor_ipv4_to_int "$ip")" || return 1
  address_int="$(manor_ipv4_to_int "$address")" || return 1
  mask=$(( (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF ))
  (( (ip_int & mask) == (address_int & mask) ))
}

manor_validate_expected_pod_cidr() {
  local value="$1"
  local environment="$2"
  if ! manor_validate_private_ipv4_cidr "$value"; then
    echo "EXPECTED_POD_CIDR must be the canonical private IPv4 Pod CIDR for ${environment}, with prefix /16 or narrower." >&2
    return 1
  fi
}

manor_validate_forwarded_allow_ips() {
  local value="$1"
  local environment="$2"
  local expected_pod_cidr="$3"
  local token peer cidr
  local has_loopback=false
  local has_proxy_cidr=false
  local covered
  local -a tokens proxy_cidrs
  if [[ -z "$value" || "$value" == ,* || "$value" == *, || "$value" == *,,* ]]; then
    echo "FORWARDED_ALLOW_IPS contains an empty proxy entry for ${environment}." >&2
    return 1
  fi
  manor_validate_expected_pod_cidr "$expected_pod_cidr" "$environment" || return 1
  IFS=, read -r -a tokens <<<"$value"
  if [[ "${#tokens[@]}" -ne 2 ]]; then
    echo "FORWARDED_ALLOW_IPS must contain only 127.0.0.1 and expected Pod CIDR ${expected_pod_cidr} for ${environment}." >&2
    return 1
  fi
  for token in "${tokens[@]}"; do
    token="$(manor_trim "$token")"
    if [[ "$token" == "127.0.0.1" ]]; then
      has_loopback=true
      continue
    fi
    if ! manor_validate_private_ipv4_cidr "$token"; then
      echo "FORWARDED_ALLOW_IPS must contain the actual ${environment} ingress Pod CIDR as canonical private IPv4 CIDRs with prefix /16 or narrower." >&2
      return 1
    fi
    if [[ "$token" != "$expected_pod_cidr" ]]; then
      echo "FORWARDED_ALLOW_IPS proxy CIDR must exactly equal expected Pod CIDR ${expected_pod_cidr} for ${environment}." >&2
      return 1
    fi
    has_proxy_cidr=true
    proxy_cidrs+=("$token")
  done
  if [[ "$has_loopback" != "true" || "$has_proxy_cidr" != "true" ]]; then
    echo "FORWARDED_ALLOW_IPS must contain 127.0.0.1 and the actual ${environment} ingress Pod CIDR." >&2
    return 1
  fi
  shift 3
  for peer in "$@"; do
    manor_ipv4_to_int "$peer" >/dev/null || {
      echo "Ingress controller reported invalid IPv4 Pod address: ${peer}." >&2
      return 1
    }
    covered=false
    for cidr in "${proxy_cidrs[@]}"; do
      if manor_ipv4_in_cidr "$peer" "$cidr"; then
        covered=true
        break
      fi
    done
    if [[ "$covered" != "true" ]]; then
      echo "FORWARDED_ALLOW_IPS must contain the actual ${environment} ingress Pod CIDR; controller Pod ${peer} is not trusted." >&2
      return 1
    fi
  done
}

manor_validate_live_ingress_proxy_trust() {
  local value="$1"
  local environment="$2"
  local expected_pod_cidr="$3"
  local ingress_namespace="$4"
  local pod_ip
  local -a pod_ips
  pod_ips=()
  while IFS= read -r pod_ip; do
    [[ -n "$pod_ip" ]] && pod_ips+=("$pod_ip")
  done < <(
    kubectl -n "$ingress_namespace" get pods \
      -l app.kubernetes.io/component=controller \
      --field-selector=status.phase=Running \
      -o 'jsonpath={range .items[*]}{.status.podIP}{"\n"}{end}'
  )
  if [[ "${#pod_ips[@]}" -eq 0 ]]; then
    echo "No Running ingress controller Pods found in namespace ${ingress_namespace}." >&2
    return 1
  fi
  manor_validate_forwarded_allow_ips \
    "$value" "$environment" "$expected_pod_cidr" "${pod_ips[@]}"
}

manor_force_external_secret_refresh() {
  local namespace="$1"
  local name="$2"
  local sync_token snapshot observed_resource_version baseline_synced_resource_version
  local annotated_resource_version synced_resource_version ready attempt
  sync_token="$(date +%s)-$$"
  annotated_resource_version=""
  for attempt in $(seq 1 36); do
    snapshot="$(
      kubectl -n "$namespace" get "externalsecret/${name}" \
        -o 'jsonpath={.metadata.resourceVersion}{"|"}{.status.syncedResourceVersion}' \
        2>/dev/null || true
    )"
    observed_resource_version="${snapshot%%|*}"
    baseline_synced_resource_version="${snapshot#*|}"
    if [[ -n "$observed_resource_version" ]]; then
      annotated_resource_version="$(
        kubectl -n "$namespace" annotate "externalsecret/${name}" \
          "force-sync=${sync_token}" --overwrite \
          --resource-version="$observed_resource_version" \
          -o 'jsonpath={.metadata.resourceVersion}' 2>/dev/null || true
      )"
      [[ -n "$annotated_resource_version" ]] && break
    fi
    sleep 5
  done
  if [[ -z "$annotated_resource_version" ]]; then
    echo "ExternalSecret/${name} could not be atomically annotated for force-sync." >&2
    return 1
  fi
  for attempt in $(seq 1 36); do
    snapshot="$(
      kubectl -n "$namespace" get "externalsecret/${name}" \
        -o 'jsonpath={.status.syncedResourceVersion}{"|"}{.status.conditions[?(@.type=="Ready")].status}' \
        2>/dev/null || true
    )"
    synced_resource_version="${snapshot%%|*}"
    ready="${snapshot#*|}"
    if [[ "$ready" == "True" && -n "$synced_resource_version" \
      && "$synced_resource_version" != "$baseline_synced_resource_version" ]] \
      && kubectl -n "$namespace" get "secret/${name}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  kubectl -n "$namespace" get "externalsecret/${name}" -o yaml >&2 || true
  echo "Timed out waiting for ExternalSecret/${name} to refresh." >&2
  return 1
}

manor_runtime_secret_revision() {
  local namespace="$1"
  local revision
  revision="$(
    kubectl -n "$namespace" get secret/manor-runtime-secret \
      -o 'jsonpath={.metadata.resourceVersion}'
  )"
  if [[ -z "$revision" ]]; then
    echo "secret/manor-runtime-secret did not report a resourceVersion." >&2
    return 1
  fi
  printf '%s\n' "$revision"
}

manor_rollout_runtime_secret_revision() {
  local namespace="$1"
  local revision="$2"
  shift 2
  local deployment
  for deployment in "$@"; do
    kubectl -n "$namespace" patch "deployment/${deployment}" --type=merge \
      -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"manor.ai/runtime-secret-revision\":\"${revision}\"}}}}}" \
      >/dev/null
  done
}
