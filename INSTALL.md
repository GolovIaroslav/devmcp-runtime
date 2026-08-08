# DevMCP Runtime installation and setup

This document covers installation, CLI usage, systemd daemon setup, and client
configuration for DevMCP Runtime.

## Requirements

- Linux with bubblewrap (`bwrap`) for the supported beta security boundary
- Python ≥ 3.11
- `uv` (recommended) or standard `pip`

## Quickstart

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/GolovIaroslav/test.git devmcp-runtime
cd devmcp-runtime
uv sync --extra dev
uv run devmcp setup --workspace /path/to/your/repo --no-tunnel
```

### 2. Run the configured local MCP service

```bash
uv run devmcp serve
```

### 3. Check the local endpoint

```bash
uv run devmcp status
```

## Systemd Daemon Installation (Linux)

To run the runtime automatically in the background as a user service, keep the
installed runtime tree separate from the project it is allowed to edit:

```bash
devmcp setup --workspace "$HOME/Documents/projects/my-project" --no-tunnel
devmcp service install
```

The installer requires an explicit workspace, creates only a user systemd
unit, and generates loopback bearer authentication when no token is supplied.
The MCP runtime source directory is never selected as the authoritative
workspace automatically.

To inspect service logs:
```bash
journalctl --user -u devmcp-runtime.service -f
```

## Out-of-Band Approval CLI (`devmcp`)

When an untrusted shell execution is attempted by an AI agent, the runtime returns an approval challenge.

To view pending approvals:
```bash
devmcp approvals
```

To approve a request:
```bash
devmcp approve <approval_id>
```

To deny a request:
```bash
devmcp deny <approval_id>
```
