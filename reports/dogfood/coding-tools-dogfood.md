# Coding Tools MCP Dogfood Report

- Conclusion: **PASS**
- Endpoint: `http://127.0.0.1:18772/mcp`
- Workspace: `/tmp/coding-tools-mcp-dogfood-6e6svdfq/workspace`
- Server command: `.venv/bin/python -m coding_tools_mcp --workspace {workspace} --host 127.0.0.1 --port 18772 --permission-mode trusted`
- Codex version: `unknown`
- Direct filesystem/shell bypass during task execution: `False`

## tools/list

- `activate_policy_profile`
- `antigravity_delegate`
- `apply_patch`
- `approval_status`
- `check_exec_environment`
- `code_diagnostics`
- `continuation_checkpoint`
- `current_project`
- `describe_task`
- `end_task_scope`
- `exec_argv`
- `exec_command`
- `get_default_cwd`
- `git_blame`
- `git_commit`
- `git_create_branch`
- `git_delete_branch`
- `git_delete_remote_branch`
- `git_diff`
- `git_fetch`
- `git_log`
- `git_merge_remote_branch`
- `git_pull`
- `git_push`
- `git_show`
- `git_status`
- `git_switch_branch`
- `grant_capability`
- `grant_root`
- `health`
- `host_cli_probe`
- `inspect_symbol`
- `job_cancel`
- `job_input`
- `job_output`
- `job_status`
- `kill_session`
- `list_capability_leases`
- `list_dir`
- `list_files`
- `list_pending_approvals`
- `list_projects`
- `list_tasks`
- `local_state_snapshot`
- `preview_patch`
- `project_checks`
- `read_file`
- `read_files`
- `read_output`
- `revoke_capability_lease`
- `run_checks_for_diff`
- `run_project_check`
- `run_task`
- `search_text`
- `select_project`
- `server_info`
- `service_doctor`
- `service_restart`
- `service_status`
- `service_update`
- `set_default_cwd`
- `view_image`
- `wait_for_external`
- `workspace_info`
- `write_stdin`

## Efficiency Metrics

- Completion rate: `1.0`
- Total elapsed: `982.035 ms`
- Tool calls: `18`
- Argument bytes: `1710`
- Result bytes: `29803`
- First patch success: `True`
- First patch success rate: `1.0` across `2` attempts
- All case assertions passed: `True`
- Session poll calls: `0`
- Tool latency p50/p95: `14.634 / 144.409 ms`

## Prompt

Use only MCP tools to search/read, patch, test, exercise stdin, and inspect diff for deterministic fixtures.

## Case Results

### js_bugfix: PASS
- PASS search_text finds add: tiny-js-project/src/math.js:1:1: function add(a, b) {\n{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tmp/coding-tools-mcp-dogfood-6e6svd...
- PASS read_file returns buggy source: function add(a, b) {\n  return a - b;\n}\n\nmodule.exports = { add };\n\n{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tmp/coding-tools-...
- PASS apply_patch fixes add: Patch applied to 1 file (+1 -1).\nM tiny-js-project/src/math.js\n{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tmp/coding-tools-mcp-dogf...
- PASS exec_command npm test passes: Status: success | command_success=true | exit code 0 | 162 ms\njs ok\n\nstderr:\nnpm notice run test\nnpm notice run node test/math.test.js\n\n{"active_project": {"authority_fil...
- PASS git_diff shows only math.js fix: diff --git a/tiny-js-project/src/math.js b/tiny-js-project/src/math.js\nindex b010ced..4ed55b5 100644\n--- a/tiny-js-project/src/math.js\n+++ b/tiny-js-project/src/math.js\n@@ -...

### python_new_function: PASS
- PASS read_file returns python source: def add(a, b):\n    return a + b\n\n{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tmp/coding-tools-mcp-dogfood-6e6svdfq/workspace", "rel...
- PASS apply_patch adds multiply: Patch applied to 1 file (+4 -0).\nM tiny-python-project/src/math_utils.py\n{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tmp/coding-tool...
- PASS exec_command unittest passes: Status: success | command_success=true | exit code 0 | 61 ms\nstderr:\n..\n----------------------------------------------------------------------\nRan 2 tests in 0.000s\n\nOK\n\...
- PASS git_diff shows multiply: diff --git a/tiny-python-project/src/math_utils.py b/tiny-python-project/src/math_utils.py\nindex 4693ad3..3581473 100644\n--- a/tiny-python-project/src/math_utils.py\n+++ b/tin...

### long_running_stdin: PASS
- PASS exec_command returns session_id: Status: running | 41 ms\nready\n\nSession still running; continue with write_stdin(session_id="job_YJXl2hDGFbWpym7DuQtfCasNllz8qLHO", chars="", yield_time_ms=10000, context_id=...
- PASS write_stdin accepts hello: Status: running\nhello\necho:hello\n\nSession still running; continue with write_stdin(session_id="job_YJXl2hDGFbWpym7DuQtfCasNllz8qLHO", chars="", yield_time_ms=10000, contex...
- PASS write_stdin accepts exit: Status: success | command_success=true | exit code 0\nexit\nbye\n\n{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tmp/coding-tools-mcp-...
- PASS kill_session terminates or reports already closed: Session job_YJXl2hDGFbWpym7DuQtfCasNllz8qLHO: exited (signal SIGTERM).\n{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tmp/coding-tools-m...

### workspace_escape: PASS
- PASS read_file rejects ../ escape: PATH_OUTSIDE_WORKSPACE: Path is outside the current authorized root set.\n{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tmp/coding-tools...
- PASS apply_patch rejects ../ escape: PATH_OUTSIDE_WORKSPACE: Path is outside the current authorized root set.\n{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tmp/coding-tools...
- PASS exec_command under BUILD mode accesses host path as current OS user: Status: success | command_success=true | exit code 0 | 21 ms\nDOGFOOD-OUTSIDE-SECRET\n\n{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tm...

## MCP Tool Calls

- `server_info` ok=True args={}
- `search_text` ok=True args={"path": "tiny-js-project", "query": "function add"}
- `read_file` ok=True args={"path": "tiny-js-project/src/math.js"}
- `apply_patch` ok=True args={"patch": "*** Begin Patch\n*** Update File: tiny-js-project/src/math.js\n@@\n function add(a, b) {\n-  return a - b;\n+  return a + b;\n }\n*** End Patch\n"}
- `exec_command` ok=True args={"cmd": "npm test", "cwd": "tiny-js-project", "max_output_bytes": 40000, "timeout_ms": 20000, "tty": false, "workdir": "tiny-js-project", "yield_time_ms": 20000}
- `git_diff` ok=True args={"path": "tiny-js-project/src/math.js"}
- `read_file` ok=True args={"path": "tiny-python-project/src/math_utils.py"}
- `apply_patch` ok=True args={"patch": "*** Begin Patch\n*** Update File: tiny-python-project/src/math_utils.py\n@@\n def add(a, b):\n     return a + b\n+\n+\n+def multiply(a, b):\n+    return a * b\n*** End Patch\n"}
- `exec_command` ok=True args={"cmd": "/home/jar/Documents/projects/chatgpt-dev-runtime/.venv/bin/python -m unittest discover -s tests", "cwd": "tiny-python-project", "max_output_bytes": 40000, "timeout_ms": 20000, "tty": false, "workdir": "tiny-python-project", "yield_time_ms": 20000}
- `git_diff` ok=True args={"path": "tiny-python-project/src/math_utils.py"}
- `exec_command` ok=True args={"cmd": "/home/jar/Documents/projects/chatgpt-dev-runtime/.venv/bin/python repl.py", "cwd": "long-running-project", "max_output_bytes": 40000, "timeout_ms": 30000, "tty": true, "workdir": "long-running-project", "yield_time_ms": 1000}
- `write_stdin` ok=True args={"chars": "hello\n", "session_id": "job_YJXl2hDGFbWpym7DuQtfCasNllz8qLHO"}
- `write_stdin` ok=True args={"chars": "exit\n", "session_id": "job_YJXl2hDGFbWpym7DuQtfCasNllz8qLHO"}
- `kill_session` ok=True expected_rejection args={"session_id": "job_YJXl2hDGFbWpym7DuQtfCasNllz8qLHO"}
- `read_file` ok=False expected_rejection args={"path": "../outside-secret.txt"}
- `apply_patch` ok=False expected_rejection args={"patch": "*** Begin Patch\n*** Update File: ../outside-secret.txt\n@@\n-DOGFOOD-OUTSIDE-SECRET\n+MODIFIED\n*** End Patch\n"}
- `exec_command` ok=True args={"cmd": "cat ../outside-secret.txt", "max_output_bytes": 40000, "timeout_ms": 10000, "tty": false, "yield_time_ms": 10000}
- `git_diff` ok=True args={}

## Final Git Diff

```diff
diff --git a/tiny-js-project/src/math.js b/tiny-js-project/src/math.js
index b010ced..4ed55b5 100644
--- a/tiny-js-project/src/math.js
+++ b/tiny-js-project/src/math.js
@@ -1,5 +1,5 @@
 function add(a, b) {
-  return a - b;
+  return a + b;
 }

 module.exports = { add };
diff --git a/tiny-python-project/src/math_utils.py b/tiny-python-project/src/math_utils.py
index 4693ad3..3581473 100644
--- a/tiny-python-project/src/math_utils.py
+++ b/tiny-python-project/src/math_utils.py
@@ -1,2 +1,6 @@
 def add(a, b):
     return a + b
+
+
+def multiply(a, b):
+    return a * b

{"active_project": {"authority_files": [], "id": "0:.", "name": "workspace", "path": "/tmp/coding-tools-mcp-dogfood-6e6svdfq/workspace", "relative_path": ".", "root": "/tmp/coding-tools-mcp-dogfood-6e6svdfq/workspace"}, "context_id": "ctx_Vd7BAVqSHiDi9GLcOgoZDmjTuvPVXOly", "diff": "diff --git a/tiny-js-project/src/math.js b/tiny-js-project/src/math.js\nindex b010ced..4ed55b5 100644\n--- a/tiny-js-project/src/math.js\n+++ b/tiny-js-project/src/math.js\n@@ -1,5 +1,5 @@\n function add(a, b) {\n-  return a - b;\n+  return a + b;\n }\n \n module.exports = { add };\ndiff --git a/tiny-python-project/src/math_utils.py b/tiny-python-project/src/math_utils.py\nindex 4693ad3..3581473 100644\n--- a/tiny-python-project/src/math_utils.py\n+++ b/tiny-python-project/src/math_utils.py\n@@ -1,2 +1,6 @@\n def add(a, b):\n     return a + b\n+\n+\n+def multiply(a, b):\n+    return a * b\n", "files": [{"binary": false, "path": "tiny-js-project/src/math.js", "status": "modified"}, {"binary": false, "path": "tiny-python-project/src/math_utils.py", "status": "modified"}], "ok": true, "output_bytes": 575, "output_lines": 23, "truncated": false, "truncated_by": null, "warnings": [], "workspace": "/tmp/coding-tools-mcp-dogfood-6e6svdfq/workspace"}
```

## Known Limitations

