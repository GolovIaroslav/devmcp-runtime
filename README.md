# ChatGPT Dev Runtime

ChatGPT Dev Runtime is a secure, local MCP-based coding runtime designed specifically to turn ChatGPT into a powerful coding agent while protecting your production codebase.

## Features

- **Strict Sandbox Execution**: All commands and destructive patches are executed inside an ephemeral sandbox directory. 
- **Approval Engine**: Destructive or unknown commands prompt a side-channel approval challenge (`devmcp approve <id>`).
- **Antigravity SDK Support**: Subagent delegation is wired directly through the runtime using Google's Antigravity framework.
- **Task Registry**: Pre-bundled task templates for common project lifecycles (tests, linters, builds).
- **Core Constraints**: 
  - `Delete File` and `Move to` are completely disabled. 
  - `.env` files and `*.key`/`*.pem` are hidden from the agent.
  - Landlock Kernel constraints isolate sandbox writes.

## Architecture

1. **Authoritative Workspace**: The real user directory on disk. Safe reads, limited atomic appends.
2. **Execution Sandbox**: An ephemeral copy generated on session start where `exec_command` runs. 
3. **Approval API**: Blocks MCP execution until `devmcp approve <id>` is executed by the human on the terminal.

## Installation

You can install the runtime via `uv` or standard Python:

```bash
uv sync
./scripts/install_systemd.sh
```

## Tools

The agent gets access to a curated strict toolset to read files, examine git, list directory info, and invoke sandbox/antigravity commands.

- `read_file`, `list_dir`, `search_text`
- `exec_command` (Sandboxed)
- `apply_patch` (Strict additions/updates only)
- `list_tasks`, `run_task`
- `antigravity_start`
