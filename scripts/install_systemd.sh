#!/usr/bin/env bash
set -e

SERVICE_NAME="chatgpt-dev-runtime"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root to install systemd service"
  exit 1
fi

PROJECT_DIR=$(pwd)
REAL_USER=${SUDO_USER:-$(whoami)}

echo "Creating systemd service for ${SERVICE_NAME} at ${SERVICE_FILE}..."

cat <<EOF > ${SERVICE_FILE}
[Unit]
Description=ChatGPT Dev MCP Runtime
After=network.target

[Service]
Type=simple
User=${REAL_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/.venv/bin/python3 -m coding_tools_mcp --workspace ${PROJECT_DIR} --host 127.0.0.1 --port 47157
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd daemon..."
systemctl daemon-reload
echo "Enabling ${SERVICE_NAME} service..."
systemctl enable ${SERVICE_NAME}
echo "Starting ${SERVICE_NAME} service..."
systemctl start ${SERVICE_NAME}

echo "${SERVICE_NAME} service installed and started successfully!"
