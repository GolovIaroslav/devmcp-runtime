#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "run this installer as the normal user; it creates user services only" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RUNTIME_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
WORKSPACE="${CODING_TOOLS_MCP_WORKSPACE:-/home/jar/Documents/projects/chatgpt-mcp-playground}"
PYTHON="${CODING_TOOLS_MCP_PYTHON:-${RUNTIME_DIR}/.venv/bin/python3}"
TUNNEL_BIN="${TUNNEL_CLIENT_BIN:-${HOME}/.local/bin/tunnel-client}"
TUNNEL_ID="${TUNNEL_CLIENT_TUNNEL_ID:-tunnel_6a771229f2e48191b34d642ea92892c8}"
ALIAS="${TUNNEL_CLIENT_RUNTIME_ALIAS:-chatgpt-dev-runtime}"
PROFILE="${TUNNEL_CLIENT_PROFILE:-sample_mcp_with_dcr}"
PROFILE_DIR="${TUNNEL_CLIENT_PROFILE_DIR:-${HOME}/.config/tunnel-client}"
MCP_URL="${CODING_TOOLS_MCP_URL:-http://127.0.0.1:47157/mcp}"
RUNTIME_API_KEY="file:${HOME}/.config/tunnel-client/control-plane-api-key"
TOKEN_FILE="${HOME}/.config/chatgpt-dev-runtime/mcp-token"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"

for required in "${WORKSPACE}" "${RUNTIME_DIR}" "${PYTHON}" "${TUNNEL_BIN}" "${TOKEN_FILE}"; do
  if [[ ! -e "${required}" ]]; then
    echo "required path is missing: ${required}" >&2
    exit 2
  fi
done
if [[ ! -x "${PYTHON}" || ! -x "${TUNNEL_BIN}" ]]; then
  echo "python and tunnel-client must be executable" >&2
  exit 2
fi
if [[ ! -s "${TOKEN_FILE}" ]]; then
  echo "MCP bearer file is missing or empty: ${TOKEN_FILE}" >&2
  exit 2
fi
if [[ ! -d "${WORKSPACE}" || ! -d "${RUNTIME_DIR}" ]]; then
  echo "runtime and authoritative workspace must be directories" >&2
  exit 2
fi

RUNTIME_REAL="$(cd -- "${RUNTIME_DIR}" && pwd -P)"
WORKSPACE_REAL="$(cd -- "${WORKSPACE}" && pwd -P)"
if [[ "${RUNTIME_REAL}" == "${WORKSPACE_REAL}" || "${RUNTIME_REAL}/" == "${WORKSPACE_REAL}/"* || "${WORKSPACE_REAL}/" == "${RUNTIME_REAL}/"* ]]; then
  echo "runtime source and authoritative workspace must remain separate" >&2
  exit 2
fi

mkdir -p "${USER_SYSTEMD_DIR}"
chmod 700 "${HOME}/.config" "${HOME}/.config/chatgpt-dev-runtime" "${HOME}/.config/tunnel-client" 2>/dev/null || true
chmod 600 "${TOKEN_FILE}" 2>/dev/null || true

sed \
  -e "s|@RUNTIME_DIR@|${RUNTIME_REAL}|g" \
  -e "s|@WORKSPACE@|${WORKSPACE_REAL}|g" \
  -e "s|@PYTHON@|${PYTHON}|g" \
  "${RUNTIME_DIR}/systemd/chatgpt-dev-runtime.service.in" \
  > "${USER_SYSTEMD_DIR}/chatgpt-dev-runtime.service"
sed \
  -e "s|@RUNTIME_DIR@|${RUNTIME_REAL}|g" \
  -e "s|@TUNNEL_BIN@|${TUNNEL_BIN}|g" \
  -e "s|@ALIAS@|${ALIAS}|g" \
  -e "s|@TUNNEL_ID@|${TUNNEL_ID}|g" \
  -e "s|@PROFILE@|${PROFILE}|g" \
  -e "s|@PROFILE_DIR@|${PROFILE_DIR}|g" \
  -e "s|@MCP_URL@|${MCP_URL}|g" \
  -e "s|@RUNTIME_API_KEY@|${RUNTIME_API_KEY}|g" \
  "${RUNTIME_DIR}/systemd/tunnel-client-chatgpt-dev-runtime.service.in" \
  > "${USER_SYSTEMD_DIR}/tunnel-client-chatgpt-dev-runtime.service"
chmod 600 "${USER_SYSTEMD_DIR}/chatgpt-dev-runtime.service" "${USER_SYSTEMD_DIR}/tunnel-client-chatgpt-dev-runtime.service"

systemctl --user daemon-reload
systemctl --user enable chatgpt-dev-runtime.service tunnel-client-chatgpt-dev-runtime.service
echo "installed user services for MCP runtime and Secure MCP Tunnel"
echo "start with: devmcp start"
