#!/usr/bin/env bash
set -euo pipefail

BIN="${HOME}/.local/bin/tunnel-client"
ALIAS="chatgpt-dev-runtime"
TUNNEL_ID="tunnel_6a771229f2e48191b34d642ea92892c8"
PROFILE="sample_mcp_with_dcr"
PROFILE_DIR="${HOME}/.config/tunnel-client"
MCP_URL="http://127.0.0.1:47157/mcp"
RUNTIME_API_KEY="file:${HOME}/.config/tunnel-client/control-plane-api-key"
PYTHON="python3"

while (($#)); do
  case "$1" in
    --bin) BIN="${2:?--bin requires a path}"; shift 2 ;;
    --alias) ALIAS="${2:?--alias requires a value}"; shift 2 ;;
    --tunnel-id) TUNNEL_ID="${2:?--tunnel-id requires a value}"; shift 2 ;;
    --profile) PROFILE="${2:?--profile requires a value}"; shift 2 ;;
    --profile-dir) PROFILE_DIR="${2:?--profile-dir requires a path}"; shift 2 ;;
    --mcp-url) MCP_URL="${2:?--mcp-url requires a URL}"; shift 2 ;;
    --runtime-api-key) RUNTIME_API_KEY="${2:?--runtime-api-key requires a reference}"; shift 2 ;;
    --python) PYTHON="${2:?--python requires a path}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -x "${BIN}" ]]; then
  echo "tunnel-client binary not found: ${BIN}" >&2
  exit 2
fi

while true; do
  "${BIN}" runtimes connect \
    --alias "${ALIAS}" \
    --tunnel-id "${TUNNEL_ID}" \
    --profile "${PROFILE}" \
    --profile-dir "${PROFILE_DIR}" \
    --runtime-api-key "${RUNTIME_API_KEY}" \
    --mcp-server-url "${MCP_URL}" \
    --tunnel-client-bin "${BIN}"

  while true; do
    status_json="$("${BIN}" runtimes status "${ALIAS}" --json 2>/dev/null)" || break
    if ! printf '%s' "${status_json}" | "${PYTHON}" -c 'import json, sys; d=json.load(sys.stdin); print("ok" if d.get("process_running") and d.get("healthy") else "wait")' | grep -qx ok; then
      break
    fi
    sleep 5
  done

  "${BIN}" runtimes stop "${ALIAS}" >/dev/null 2>&1 || true
  sleep 2
done
