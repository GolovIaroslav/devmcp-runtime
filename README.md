# ChatGPT Dev Runtime

ChatGPT Dev Runtime is a secure, local MCP-based coding runtime designed specifically to turn ChatGPT into a powerful coding agent while protecting your production codebase.

## Quickstart

Get up and running with the agent quickly and securely.

## Safety Boundary

Our robust sandbox protects your host system from destructive actions.

## Dogfood

We heavily dogfood the runtime with our internal development.

## Features

- **Strict Sandbox Execution**: Registered tests and commands run in an ephemeral sandbox with default network isolation.
- **Approval Engine**: Risky operations return an out-of-band approval record (`devmcp approve <id>`); ordinary safe patches and registered tasks run automatically.
- **Antigravity SDK Support**: Subagent delegation is wired directly through the runtime using Google's Antigravity framework.
- **Task Registry**: Pre-bundled task templates for common project lifecycles (tests, linters, builds).
- **Core Constraints**: 
  - `Delete File` and `Move to` are completely disabled. 
  - `.env` files and `*.key`/`*.pem` are hidden from the agent.
  - Landlock Kernel constraints isolate sandbox writes.
- **SWE-bench Compatible**: Configured to run seamless eval suites.

## Architecture

1. **Authoritative Workspace**: The explicitly selected user directory on disk. Safe reads, previews, and small atomic Add/Update patches are allowed.
2. **Execution Sandbox**: An ephemeral copy generated on session start where `exec_command` runs. 
3. **Approval API**: Only risky operations pause for `devmcp approve <id>`; the model retries the exact operation after local approval.

## Installation

You can install the runtime via `uv` or standard Python:

```bash
uv sync
./scripts/install_systemd.sh
```

The installer creates user services only. Use `devmcp status`, `devmcp start`,
`devmcp stop`, `devmcp restart`, and `devmcp logs` for local supervision; it
does not expose these security-management commands to the model.

## Tools

The agent gets access to a curated strict toolset to read files, examine git, list directory info, and invoke sandbox/antigravity commands.

- `read_file`, `list_dir`, `search_text`
- `exec_command` (Sandboxed)
- `apply_patch` (Strict additions/updates only)
- `list_tasks`, `run_task`
