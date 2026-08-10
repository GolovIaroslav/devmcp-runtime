# Tools And Schemas

The normative behavior is [runtime-contract-v0.2.md](runtime-contract-v0.2.md).
Live JSON Schemas come from `tools/list`; CI compares their names, input
properties, annotations, and error codes with the contract.

The server metadata exposes tool schema version `1.0`. Additive optional fields
are preferred. A breaking tool name, required-input, or annotation change must
be documented as requiring a ChatGPT Developer Mode app **Refresh** while the
app remains a draft.

## Fixed inventory

The default catalog contains exactly 48 tools:

- `server_info`: Server info.
- `health`: Health check.
- `workspace_info`: Workspace info.
- `service_status`: Run host-side DevMCP status diagnostics.
- `service_doctor`: Run host-side DevMCP doctor diagnostics.
- `service_restart`: Schedule a delayed restart of the DevMCP user services.
- `list_projects`: Discover Git repositories under operator-approved project roots.
- `select_project`: Select one writable repository for the current MCP session.
- `current_project`: Show the repository selected for the current MCP session.
- `project_checks`: Discover bounded project-native verification commands.
- `run_project_check`: Run one discovered project-native check in the sandbox.
- `read_file`: Read file.
- `read_files`: Read multiple files.
- `list_dir`: List directory.
- `list_files`: List files.
- `search_text`: Search text.
- `view_image`: View image.
- `preview_patch`: Preview patch.
- `apply_patch`: Apply patch.
- `git_status`: Git status.
- `git_diff`: Git diff.
- `git_log`: Git log.
- `git_show`: Git show.
- `git_blame`: Git blame.
- `git_create_branch`: Create and switch to a local branch in the selected repository.
- `git_switch_branch`: Switch to an existing local branch in the selected repository.
- `git_fetch`: Fetch and prune one configured remote.
- `git_pull`: Fast-forward only the current branch from one configured remote.
- `git_delete_branch`: Safely delete one merged local branch.
- `git_delete_remote_branch`: Delete one branch from a configured remote.
- `git_commit`: Commit only explicitly named paths.
- `git_push`: Push the current branch to a configured remote; force is rejected.
- `list_tasks`: List tasks.
- `describe_task`: Describe task.
- `run_task`: Run task.
- `exec_command`: Exec command.
- `job_status`: Job status.
- `read_output`: Read output.
- `write_stdin`: Write stdin.
- `kill_session`: Kill session.
- `job_output`: Job output.
- `job_input`: Job input.
- `job_cancel`: Job cancel.
- `approval_status`: Approval status.
- `list_pending_approvals`: List pending approvals.
- `check_exec_environment`: Check exec environment.
- `get_default_cwd`: Get default cwd.
- `set_default_cwd`: Set default cwd.
`view_image` may be disabled when an installation cannot accept binary image
content. That capability gate is not a tool profile. The other 47 tools are
always advertised, and `listChanged` is `false`.

`service_status` and `service_doctor` execute only fixed DevMCP operator
commands on the host; they do not accept arbitrary command text. `service_restart`
is controlled by the `service.manage` policy capability and uses a delayed
user-systemd transient unit so the current MCP response can complete before the
running service is replaced.

## Project selection boundary

Operator-configured `workspaces` are passed to the runtime as project discovery
roots. `list_projects` recursively discovers Git repositories without following
symlinks, and `select_project` changes only the current MCP session's active
repository. It never persists a new root. Once selected, direct file tools,
patches, sandboxed execution, project checks, and Git operations are all rooted
at that one canonical repository. Absolute paths, `..`, and symlink escapes are
rejected.

HTTP sessions own independent `Runtime` instances, so two ChatGPT sessions can
select different repositories without a process-global mutable selector. The
default mode works directly against the existing checkout; selecting a project
does not create a worktree.

## First-class Git mutations

Branch create/switch/delete, fetch/prune, fast-forward-only pull, explicit-path
commit, push, and remote-branch deletion are host-side Git
operations scoped to the selected repository rather than generic sandbox tasks.
`git_commit` requires a non-empty path list, rejects unrelated pre-staged paths,
and does not run `git add -A`. `git_push` accepts only a configured remote name,
rejects force push, withholds raw push output on failure, and remains controlled
by the `git.push` policy capability.

`git_fetch` always uses `--prune`; `git_pull` always uses `--ff-only` and refuses
to run with tracked or staged worktree changes. Both are controlled by
`git.sync`. Local branch deletion uses only `git branch -d`, never `-D`. Remote
branch deletion accepts only a configured remote and is controlled by
`git.push`.

The generic task registry intentionally does not advertise Git commands because
the execution sandbox does not expose repository Git metadata. Read-only Git
inspection and mutations use the dedicated Git tools instead.

## Project-native verification

`project_checks` prefers repository-owned Makefile gates when present. For a
Python project with `uv.lock` and `pyproject.toml`, the fallback test command is
`uv run --offline --frozen --no-sync python -m pytest`; it never silently falls
back to a host bare `pytest`. Existing `.venv` Python is used only when no uv
lock is present. `run_project_check` executes only a discovered argv in the
normal repository sandbox and does not auto-install dependencies or enable
network access.

## Result envelope

Every successful tool call has:

```json
{
  "content": [{"type": "text", "text": "Agent-readable summary or bounded preview"}],
  "structuredContent": {"ok": true},
  "isError": false
}
```

`content` is not a JSON mirror. `structuredContent` is the complete machine
interface and retains existing fields where possible. Model-facing text is
bounded at 16 KiB; if it is shortened, the full structured value is still
present. Errors use the same envelope with readable recovery guidance and
`isError: true`.

`view_image` is the exception to text-only content: its base64 appears exactly
once in one `image` block. `structuredContent` contains path, media type, byte
count, dimensions, resize metadata, and warnings, but no base64 or data URL.

## Patch behavior

`apply_patch` accepts the standard envelope. Preview and small Add/Update
patches are automatic. Delete and Move are controlled by the active data policy:
Safe and Balanced ask, and Power can allow them. An Update is routed
to local out-of-band approval only when it removes more than 200 existing lines
or more than 30% of an existing file. Unique context, baseline/hash protection,
atomic writes, rollback, and symlink defenses apply to every patch.

```text
*** Begin Patch
*** Add File: path/to/new.py
+content
*** Update File: path/to/existing.py
@@
 old
-before
+after
*** Move to: path/to/moved.py
*** Delete File: path/to/old.py
*** End Patch
```

All operations are parsed and matched before writes. Context must be unique.
Files are prepared in their destination directories, fsynced, baseline-checked,
and installed with atomic replacement. Multi-file failure restores prior files.
Mode bits, BOM, and newline style are preserved.

## Command and output behavior

`exec_command` and `write_stdin` default `yield_time_ms` to `10000`. Short
commands ordinarily return `status: "exited"` in one call. A still-running
command returns a `session_id` and machine-readable `next_action` for
`write_stdin` with empty `chars`.

Only truncated terminal output returns a `read_output` next action by default.
`output_ref` values are `session:<id>:stdout` or `session:<id>:stderr`; offsets
are stream-specific absolute byte positions. Runtime limits bound active
commands, retained completed sessions, per-session output, total output, and
retention time.

Repository snapshots used for execution are temporary leases, not session-long
workspace copies. They live only below the runtime's private `sandboxes/`
directory. Terminal completion, failure, timeout, cancellation, and runtime
shutdown release the lease; the final lease removes the snapshot after checking
its owned path and ownership marker. A later command starts from a fresh
authoritative repository snapshot, so repeated checks do not accumulate
repository-sized `/tmp` trees.

Use `tty: true` only when a program requires a terminal. POSIX receives a real
PTY (`isatty()` is true). This build returns `TTY_UNSUPPORTED` on Windows rather
than labeling pipes as a TTY.

## Permission modes

- `safe`: allows registered non-network tasks and safe local coding operations;
  unknown commands, network, shell expansion, inline scripts, destructive
  commands, outside-workspace arguments, and secret/loader env require local
  approval or are denied according to the policy.
- `trusted`: enables normal local-development network, expansion, and inline
  snippets while retaining secret and destructive-command checks.
- `dangerous`: disables command permission gates and Landlock; use only inside
  an isolated container or VM.

These modes do not change the tool list. Direct path tools retain workspace
confinement in every mode.

`--dangerously-fake-readonly-annotations` is a fenced test/debug compatibility
switch that advertises every tool as read-only in `tools/list`. It does not stop
mutation or execution and is never recommended to avoid client prompts.
`server_info` and the server card keep reporting the real annotations. See
[permission-modes.md](permission-modes.md).
