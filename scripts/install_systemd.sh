#!/usr/bin/env bash
set -e

SERVICE_NAME="chatgpt-dev-runtime"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${USER_SYSTEMD_DIR}/${SERVICE_NAME}.service"
RUNTIME_DIR="${CODING_TOOLS_MCP_RUNTIME_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
WORKSPACE="${CODING_TOOLS_MCP_WORKSPACE:-}"
PYTHON="${CODING_TOOLS_MCP_PYTHON:-${RUNTIME_DIR}/.venv/bin/python3}"
HOST="${CODING_TOOLS_MCP_HOST:-127.0.0.1}"
PORT="${CODING_TOOLS_MCP_PORT:-47157}"

if [ "$EUID" -eq 0 ]; then
  echo "Please DO NOT run as root. This is a user-level service."
  exit 1
fi

mkdir -p "${USER_SYSTEMD_DIR}"

if [ -z "${WORKSPACE}" ]; then
  echo "Set CODING_TOOLS_MCP_WORKSPACE to the authoritative project directory." >&2
  echo "The MCP runtime source directory is never used as the workspace automatically." >&2
  exit 1
fi
if [ ! -d "${RUNTIME_DIR}" ] || [ ! -f "${RUNTIME_DIR}/coding_tools_mcp/__main__.py" ]; then
  echo "Runtime directory is not an installed coding-tools-mcp tree: ${RUNTIME_DIR}" >&2
  exit 1
fi
if [ ! -d "${WORKSPACE}" ]; then
  echo "Workspace does not exist: ${WORKSPACE}" >&2
  exit 1
fi
RUNTIME_REAL="$(cd -- "${RUNTIME_DIR}" && pwd -P)"
WORKSPACE_REAL="$(cd -- "${WORKSPACE}" && pwd -P)"
case "${RUNTIME_REAL}/" in
  "${WORKSPACE_REAL}/"*)
    echo "Runtime source must not be inside the authoritative workspace." >&2
    exit 1
    ;;
esac
case "${WORKSPACE_REAL}/" in
  "${RUNTIME_REAL}/"*)
    echo "Authoritative workspace must not be inside the runtime source." >&2
    exit 1
    ;;
esac
if [ "${RUNTIME_REAL}" = "${WORKSPACE_REAL}" ]; then
  echo "Runtime source and authoritative workspace must be different directories." >&2
  exit 1
fi
if [ ! -x "${PYTHON}" ]; then
  echo "Python executable not found: ${PYTHON}" >&2
  exit 1
fi

AUTH_TOKEN="${CODING_TOOLS_MCP_AUTH_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"
umask 077

echo "Creating systemd user service for ${SERVICE_NAME} at ${SERVICE_FILE}..."

cat <<EOF > "${SERVICE_FILE}"
[Unit]
Description=ChatGPT Dev MCP Runtime

[Service]
Type=simple
WorkingDirectory=${WORKSPACE}
Environment=CODING_TOOLS_MCP_AUTH_TOKEN=${AUTH_TOKEN}
ExecStart=${PYTHON} -m coding_tools_mcp --workspace ${WORKSPACE} --host ${HOST} --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

chmod 600 "${SERVICE_FILE}"

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload
echo "Enabling ${SERVICE_NAME} service..."
systemctl --user enable ${SERVICE_NAME}

echo "${SERVICE_NAME} user service installed successfully!"
echo "Note: The service was not started. You can start it with: systemctl --user start ${SERVICE_NAME}"
