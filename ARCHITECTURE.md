# ChatGPT Dev Runtime Architecture

## Sandboxing Strategy

To protect the authoritative workspace, sandboxed execution uses an `ExecutionSandbox` snapshot.

The `ExecutionSandbox`:
1. Copies the selected repository without following symlinks into a runtime-owned private `sandboxes/sandbox-*` directory.
2. Uses an ownership marker outside the writable snapshot so cleanup can verify exactly which temporary tree DevMCP owns.
3. Translates sandboxed command workdirs to the temporary location while the authoritative repository remains outside the execution namespace.
4. Is leased by active command sessions and deleted when the final command using it terminates, fails, times out, or is cancelled; runtime shutdown is a final cleanup path rather than the normal lifetime.
5. Is recreated from the authoritative repository for later sequential execution instead of accumulating one repository-sized copy per MCP session.

## Execution Model

DevMCP operates in two execution modes:

- **PLAN** (`--execution-mode plan`): read-only confinement. `apply_patch` and `exec_command` are denied.
- **BUILD** (`--execution-mode build`): full-access, direct OS user. Default mode.

Legacy `--permission-mode` values map at ingress: `safe` → plan, `trusted` / `dangerous` → build.

All authority is resolved once at startup by `resolve_execution_mode()`. There is no
runtime policy profile gate, no ApprovalEngine, and no in-process approval/deny decision
applied after the mode is resolved.

## Task Registry

`coding_tools_mcp/tasks.py` replaces arbitrary command guesswork with templated, deterministic tasks like `npm.test`, `project.detect`, and `pytest.all`.
