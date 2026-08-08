#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RUNTIME_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
WORKSPACE=""
PYTHON="${RUNTIME_DIR}/.venv/bin/python3"
TOKEN_FILE="${HOME}/.config/devmcp-runtime/secrets/mcp-token"

while (($#)); do
  case "$1" in
    --workspace) WORKSPACE="${2:?--workspace requires a path}"; shift 2 ;;
    --python) PYTHON="${2:?--python requires a path}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:?--token-file requires a path}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "${WORKSPACE}" || ! -d "${WORKSPACE}" ]]; then
  echo "authoritative workspace is required and must exist" >&2
  exit 2
fi
if [[ ! -x "${PYTHON}" ]]; then
  echo "python executable not found: ${PYTHON}" >&2
  exit 2
fi
if [[ ! -r "${TOKEN_FILE}" ]]; then
  echo "MCP bearer file is missing or unreadable: ${TOKEN_FILE}" >&2
  exit 2
fi

RUNTIME_REAL="$(cd -- "${RUNTIME_DIR}" && pwd -P)"
WORKSPACE_REAL="$(cd -- "${WORKSPACE}" && pwd -P)"
case "${RUNTIME_REAL}/" in
  "${WORKSPACE_REAL}/"*) echo "runtime source must be separate from authoritative workspace" >&2; exit 2 ;;
esac
case "${WORKSPACE_REAL}/" in
  "${RUNTIME_REAL}/"*) echo "authoritative workspace must be separate from runtime source" >&2; exit 2 ;;
esac

umask 077
cd -- "${RUNTIME_DIR}"
exec "${PYTHON}" -m coding_tools_mcp \
  --workspace "${WORKSPACE_REAL}" \
  --host 127.0.0.1 \
  --port "${CODING_TOOLS_MCP_PORT:-47157}" \
  --auth-token-file "${TOKEN_FILE}" \
  --policy-profile "${DEVMCP_POLICY_PROFILE:-balanced}" \
  --permission-mode safe \
  --shell-env-inherit core
