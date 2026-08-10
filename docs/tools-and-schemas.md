# Tools And Schemas

The normative behavior is [runtime-contract-v0.2.md](runtime-contract-v0.2.md).
Live JSON Schemas come from `tools/list`; CI compares their names, input
properties, annotations, and error codes with the contract.

The server metadata exposes tool schema version `1.0`. Additive optional fields
are preferred. A breaking tool name, required-input, or annotation change must
be documented as requiring a ChatGPT Developer Mode app **Refresh** while the
app remains a draft.

## Fixed inventory

The default catalog contains exactly 53 tools:

- `server_info`: Server info.
- `health`: Health check.
- `workspace_info`: Workspace info.
- `service_status`: Run host-side DevMCP status diagnostics.
- `service_doctor`: Run host-side DevMCP doctor diagnostics.
- `host_cli_probe`: Run bounded host-side `path`, `--version`, or `--help` discovery for an executable inside the selected project using a sanitized environment.
- `service_restart`: Schedule a delayed restart of the DevMCP user services.
- `service_update`: Update the installed DevMCP runtime from a clean, synced local source checkout, reinstall user services, and safely restart.
- `activate_policy_profile`: Persist a policy profile and schedule a safe restart.
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
- `git_merge_remote_branch`: Merge a fetched configured-remote branch into the current clean branch and abort automatically on conflicts.
- `git_delete_branch`: Safely delete one merged local branch.
- `git_delete_remote_branch`: Delete one branch from a configured remote.
- `git_commit`: Commit only explicitly named paths.
- `git_push`: Push the current branch to a configured remote; force is rejected.
- `antigravity_delegate`: Run one bounded Antigravity coding task in an isolated worktree.
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
content. That capability gate is not a tool profile. The other 52 tools are
always advertised, and `listChanged` is `false`.

`service_status` and `service_doctor` execute only fixed DevMCP operator
commands on the host; they do not accept arbitrary command text. `service_restart`
is controlled by the `service.manage` policy capability and uses a delayed
user-systemd transient unit so the current MCP response can complete before the
running service is replaced.

`service_update` uses the same `service.manage` capability. It accepts an
optional `source_project` selector and only considers discovered local Git
checkouts that identify themselves as the `devmcp-runtime` package. The source
must be on `main`, have no tracked or staged changes, and exactly match
`origin/main`; untracked files do not block it. A delayed updater revalidates the
full expected SHA, performs a user-level `uv tool install --force` from the local
checkout, refreshes the user systemd units from the newly installed runtime, and
then performs the safe MCP-health-before-tunnel restart sequence. This makes
future merged DevMCP releases self-updatable without an external bootstrap agent.

`activate_policy_profile` is the first-class bootstrap path for changing the
persistent host policy without arbitrary shell access. It is controlled by the
separate `policy.manage` capability, which asks in Safe, Balanced, and Power and
is automatic only in Autonomous. A successful change schedules the same safe
restart path; a restart-scheduling failure attempts to restore the previous
profile.

## Project selection boundary

Operator-configured `workspaces` are passed to the runtime as project discovery
roots. `list_projects` recursively discovers Git repositories without following
symlinks. In service-managed mode, `select_project` atomically persists the
selected canonical repository below the private DevMCP configuration directory,
so later Streamable HTTP runtimes created for the same local service continue on
that project. The persisted path must still be a Git checkout inside an
operator-configured project root; stale, invalid, or escaped values are ignored.
Direct `Runtime` instances that are not configured with an active-project state
file retain per-runtime/session selection behavior.

Once selected, direct file tools, patches, sandboxed execution, project checks,
Git operations, and bounded delegation are all rooted at that one canonical
repository. Absolute paths, `..`, and symlink escapes are rejected. Selection
works directly against the existing checkout and does not create a long-lived
worktree.

## Bounded Antigravity delegation

`antigravity_delegate` is a fallback for a coding task that cannot be completed
through a more specific DevMCP primitive. It is controlled by `agent.delegate`:
Safe, Balanced, and Power deny it; Autonomous auto-authorizes it, and Custom may
explicitly configure it. The tool runs the host `agy` binary in a temporary
detached Git worktree at the selected project's current HEAD, not in the
operator's live checkout. The live checkout must have no tracked or staged
modifications before delegation; untracked files are not copied into the
temporary worktree.

The binary is discovered from `DEVMCP_ANTIGRAVITY_BIN`, the service `PATH`,
`~/.local/bin/agy`, or `/usr/local/bin/agy`, in that order. The configured or
discovered path must be an executable file.

The delegated prompt explicitly treats repository content and tool output as
untrusted data. The host wrapper independently rejects repositories with tracked
sensitive paths, filters sensitive environment variables, disables the real Git
remote through per-process Git configuration, and requests Antigravity's sandbox
when the installed CLI advertises that option. The delegate may not commit,
change Git history, delete or move files, or modify sensitive paths. Only `M`
and `A` worktree changes survive validation; `read_only` and `verify` modes must
produce no changes. In `workspace_edit` mode, DevMCP applies the isolated binary
patch to the selected checkout only after all validations pass. Any rejected or
failed delegation is discarded with the temporary worktree.

## First-class Git mutations

Branch create/switch/delete, fetch/prune, fast-forward-only pull, explicit-path
commit, push, and remote-branch deletion are host-side Git
operations scoped to the selected repository rather than generic sandbox tasks.
`git_commit` requires a non-empty path list, rejects unrelated pre-staged paths,
and does not run `git add -A`. `git_push` accepts only a configured remote name,
rejects force push, withholds raw push output on failure, and remains controlled
by the `git.push` policy capability.

`git_fetch` always uses `--prune`; `git_pull` always uses `--ff-only` and refuses
to run with tracked or staged worktree changes. `git_merge_remote_branch` also
requires a clean tracked/staged worktree, accepts only a fetched branch from a
configured remote, and runs `git merge --abort` on failure. These operations are
controlled by `git.sync`. Local branch deletion uses only `git branch -d`, never `-D`. Remote
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

`exec_command` and `write_stdin` default `yield_time_ms` to `10000` and honor
requested initial waits up to the schema maximum of 300 seconds. This matters
for clients that create a fresh MCP runtime for each tool call and therefore
cannot reliably poll a process session owned by a prior runtime. Short
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
