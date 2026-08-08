#!/usr/bin/env bash
set -e

SERVICE_NAME="chatgpt-dev-runtime"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${USER_SYSTEMD_DIR}/${SERVICE_NAME}.service"

if [ "$EUID" -eq 0 ]; then
  echo "Please DO NOT run as root. This is a user-level service."
  exit 1
fi

mkdir -p "${USER_SYSTEMD_DIR}"
PROJECT_DIR=$(pwd)

echo "Creating systemd user service for ${SERVICE_NAME} at ${SERVICE_FILE}..."

cat <<EOF > "${SERVICE_FILE}"
[Unit]
Description=ChatGPT Dev MCP Runtime

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/.venv/bin/python3 -m coding_tools_mcp --workspace ${PROJECT_DIR} --host 127.0.0.1 --port 47157
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload
echo "Enabling ${SERVICE_NAME} service..."
systemctl --user enable ${SERVICE_NAME}

echo "${SERVICE_NAME} user service installed successfully!"
echo "Note: The service was not started. You can start it with: systemctl --user start ${SERVICE_NAME}"
