# Tools And Schemas

The normative behavior is [runtime-contract-v0.2.md](runtime-contract-v0.2.md).
Live JSON Schemas come from `tools/list`; CI compares their names, input
properties, annotations, and error codes with the contract.

## Fixed inventory

The default catalog contains exactly 32 tools:

- `server_info`: Server info.
- `health`: Health check.
- `workspace_info`: Workspace info.
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
content. That capability gate is not a tool profile. The other 31 tools are
always advertised, and `listChanged` is `false`.

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
patches are automatic; Delete and Move are always denied. An Update is routed
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

`--dangerously-fake-readonly-annotations` advertises every tool as read-only in
`tools/list` for clients that gate on annotations. It does not change the tool list
either, and it does not stop mutation or execution. `server_info` and the server
card keep reporting the real annotations. See
[permission-modes.md](permission-modes.md).
