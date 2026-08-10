# ChatGPT Dev Runtime Architecture

## Sandboxing Strategy

To protect the authoritative workspace, sandboxed execution uses an `ExecutionSandbox` snapshot.

The `ExecutionSandbox`:
1. Copies the selected repository without following symlinks into a runtime-owned private `sandboxes/sandbox-*` directory.
2. Uses an ownership marker outside the writable snapshot so cleanup can verify exactly which temporary tree DevMCP owns.
3. Translates sandboxed command workdirs to the temporary location while the authoritative repository remains outside the execution namespace.
4. Is leased by active command sessions and deleted when the final command using it terminates, fails, times out, or is cancelled; runtime shutdown is a final cleanup path rather than the normal lifetime.
5. Is recreated from the authoritative repository for later sequential execution instead of accumulating one repository-sized copy per MCP session.

## Approval Engine

The Approval Engine manages risky operations like untrusted shell execution. 

1. Tool handlers (like `exec_command`) query the `ApprovalEngine` via `evaluate_command()`.
2. Commands are flagged `ALLOW`, `DENY` (sudo, rm -rf), or `ASK`.
3. If `ASK`, a JSON response `{"status": "approval_required", "approval_id": "uuid"}` is yielded.
4. The user runs `devmcp approve <uuid>` via the CLI, rewriting state to `approvals.json`.
5. The agent retries execution successfully.

## Task Registry

`coding_tools_mcp/tasks.py` replaces arbitrary command guesswork with templated, deterministic tasks like `npm.test`, `project.detect`, and `pytest.all`.
