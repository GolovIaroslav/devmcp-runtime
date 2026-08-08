# Installation and Setup Guide

This document covers installation, CLI usage, systemd daemon setup, and client configuration for the ChatGPT Dev Runtime.

## Requirements

- Linux or macOS (Linux recommended for Landlock isolation support)
- Python ≥ 3.11
- `uv` (recommended) or standard `pip`

## Quickstart

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/GolovIaroslav/test.git chatgpt-dev-runtime
cd chatgpt-dev-runtime
uv sync
```

### 2. Run manually (stdio mode)

```bash
uv run python -m coding_tools_mcp --workspace /path/to/your/repo --stdio
```

### 3. Run manually (HTTP mode)

```bash
uv run python -m coding_tools_mcp --workspace /path/to/your/repo --host 127.0.0.1 --port 47157
```

## Systemd Daemon Installation (Linux)

To run the runtime automatically in the background as a user service, keep the
installed runtime tree separate from the project it is allowed to edit:

```bash
export CODING_TOOLS_MCP_RUNTIME_DIR="$HOME/.local/share/chatgpt-dev-runtime"
export CODING_TOOLS_MCP_WORKSPACE="$HOME/Documents/projects/my-project"
./scripts/install_systemd.sh
```

The installer requires an explicit workspace, creates only a user systemd
unit, and generates loopback bearer authentication when no token is supplied.
The MCP runtime source directory is never selected as the authoritative
workspace automatically.

To inspect service logs:
```bash
journalctl --user -u chatgpt-dev-runtime -f
```

## Out-of-Band Approval CLI (`devmcp`)

When an untrusted shell execution is attempted by an AI agent, the runtime returns an approval challenge.

To view pending approvals:
```bash
uv run python -m apps.devmcp.cli approvals
```

To approve a request:
```bash
uv run python -m apps.devmcp.cli approve <approval_id>
```

To deny a request:
```bash
uv run python -m apps.devmcp.cli deny <approval_id>
```
