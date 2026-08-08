# ChatGPT Dev Runtime Architecture

## Sandboxing Strategy

To protect the authoritative workspace, all `exec_command` calls are executed within an `ExecutionSandbox`. 

The `ExecutionSandbox`:
1. Copies the authoritative workspace to a `/tmp/chatgpt-dev-sandbox-*` path using `rsync`.
2. Intercepts `exec_command` workdirs and translates them to the temporary location.
3. Automatically deletes itself on server exit (`close()`).

## Approval Engine

The Approval Engine manages risky operations like untrusted shell execution. 

1. Tool handlers (like `exec_command`) query the `ApprovalEngine` via `evaluate_command()`.
2. Commands are flagged `ALLOW`, `DENY` (sudo, rm -rf), or `ASK`.
3. If `ASK`, a JSON response `{"status": "approval_required", "approval_id": "uuid"}` is yielded.
4. The user runs `devmcp approve <uuid>` via the CLI, rewriting state to `approvals.json`.
5. The agent retries execution successfully.

## Task Registry

`coding_tools_mcp/tasks.py` replaces arbitrary command guesswork with templated, deterministic tasks like `npm.test`, `project.detect`, and `pytest.all`.
