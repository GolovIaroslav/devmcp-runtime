# DevMCP Runtime Permissions

The runtime's execution authority is determined once at startup by a single resolver:
`resolve_execution_mode()` in `coding_tools_mcp/policy.py`.

## Execution Modes

Two modes are supported:

```text
--execution-mode plan     # read-only; apply_patch and exec_command are denied
--execution-mode build    # full-access; direct OS user (default)
```

Examples:

```bash
devmcp service update --execution-mode build   # explicit BUILD
devmcp service update                          # BUILD is the default
```

## Ingress Compatibility Adapters

The older `--permission-mode` flag remains accepted as a thin ingress adapter:

| `--permission-mode` | Maps to |
|---|---|
| `safe` | PLAN (read-only) |
| `trusted` | BUILD (full-access) |
| `dangerous` | BUILD (full-access) |

These values are resolved at startup only. They do not affect the runtime after mode
resolution.

## Runtime Access under BUILD

In BUILD mode the server runs with the permissions of the host OS user. No in-process
approval gate, command deny policy, or capability matrix is applied. Path validation
(workspace boundary, symlink rejection, NUL/traversal rejection) still applies to
direct file tools.

## Runtime Access under PLAN

In PLAN mode:
- `read_file`, `read_files`, `list_dir`, `list_files`, `search_text`, `view_image`,
  `preview_patch`, `git_status`, `git_diff`, `git_log`, `git_show`, `git_blame` — allowed
- `apply_patch` — denied (`PERMISSION_REQUIRED`)
- `exec_command` — denied (`PERMISSION_REQUIRED`)
- All other read-only tools — allowed

## Security Invariants

These apply in all modes:
- Direct file tools reject absolute paths, `..` traversal, NULs, and symlinks that
  escape the workspace.
- `exec_command` checks are OS-level, not in-process.
- Remote access requires bearer or OAuth authentication.
