# Tools And Schemas

The normative behavior is [runtime-contract-v0.2.md](runtime-contract-v0.2.md).
Live JSON Schemas come from `tools/list`; CI compares their names, input
properties, annotations, and error codes with the contract.

The server metadata exposes tool schema version `1.0`. Additive optional fields
are preferred. A breaking tool name, required-input, or annotation change must
be documented as requiring a ChatGPT Developer Mode app **Refresh** while the
app remains a draft.

## Fixed inventory

The default catalog contains exactly 57 tools:

- `server_info`: Server info.
- `health`: Health check.
- `workspace_info`: Workspace info.
- `service_status`: Run host-side DevMCP status diagnostics.
- `service_doctor`: Run host-side DevMCP doctor diagnostics.
- `host_cli_probe`: Run bounded host-side `path`, `--version`, or `--help` discovery for an executable inside the selected project using a sanitized environment.
- `service_restart`: Schedule a delayed restart of the DevMCP user services.
- `service_update`: Update the installed DevMCP runtime from a clean pinned local source checkout; explicit development mode permits a clean feature branch.
- `list_projects`: Discover Git repositories under operator-approved project roots.
- `select_project`: Select one writable repository for the current MCP session.
- `current_project`: Show the repository selected for the current MCP session.
- `local_state_snapshot`: Return project, branch, HEAD/upstream, dirty/staged paths, runtime SHA/version, and self-host state in one call.
- `project_checks`: Discover bounded project-native verification commands.
- `run_project_check`: Run one discovered project-native check using the resolved PLAN/BUILD execution model.
- `run_checks_for_diff`: Select relevant discovered checks from changed files and run them server-side in one MCP call.
- `read_file`: Read file.
- `read_files`: Read multiple files.
- `code_diagnostics`: Normalize compiler/traceback diagnostics without requiring an IDE/LSP dependency.
- `list_dir`: List directory.
- `list_files`: List files.
- `search_text`: Search text.
- `inspect_symbol`: Return a symbol definition, references, relevant tests, and bounded source context.
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
- `wait_for_external`: Wait for one bounded interval before re-polling an external system.
- `continuation_checkpoint`: Read, write, or clear one durable non-secret continuation checkpoint scoped to the selected project and logical task or branch.
- `antigravity_delegate`: Run one bounded Antigravity coding task in an isolated worktree.
- `list_tasks`: List tasks.
- `describe_task`: Describe task.
- `run_task`: Run task.
- `exec_command`: Exec command.
- `exec_argv`: Execute structured argv without shell parsing.
- `job_status`: Job status.
- `read_output`: Read output.
- `write_stdin`: Write stdin.
- `kill_session`: Kill session.
- `job_output`: Job output.
- `job_input`: Job input.
- `job_cancel`: Job cancel.
- `check_exec_environment`: Check exec environment.
- `get_default_cwd`: Get default cwd.
- `set_default_cwd`: Set default cwd.

`view_image` may be disabled when an installation cannot accept binary image
content. That capability gate is not a tool profile. The other 56 tools are
always advertised, and `listChanged` is `false`.

## Autonomous continuation primitives

`wait_for_external` deliberately does not poll GitHub, deployment providers, or
other remote APIs itself. A caller may request 1-3600 seconds, but each call has
a separate 1-90 second `timeout_seconds` hard bound and then returns an explicit
instruction to re-poll through the client's authoritative connector. This avoids
many empty model/tool round-trips without inventing credentials inside DevMCP.
The wait has no child process or repository side effect, notices request/runtime
cancellation, and leaves the service responsive to other requests.

`continuation_checkpoint` persists one small JSON record below DevMCP's private
user configuration directory, never inside the selected repository. Records
are isolated by canonical project path plus a hashed logical-task or branch
scope. Writes are atomic and mode 0600 where supported. The payload is a closed,
bounded set of continuation fields: task/slice, branch, HEAD, PR/run identifiers,
dirty-state summary, completed acceptance items, exact next action, and blocker
type. Unknown fields and oversized payloads are rejected, so callers cannot turn
the checkpoint store into an arbitrary secret or blob store. Common credential
and private-key value forms are rejected as well. `clear` removes the record
after terminal completion.

`service_status` and `service_doctor` execute only fixed DevMCP operator commands on the host; they do not accept arbitrary command text. `service_restart` uses a delayed user-systemd transient unit so the current MCP response can complete before the running service is replaced.

`service_update` accepts an
optional `source_project` selector and only considers discovered local Git
checkouts that identify themselves as the `devmcp-runtime` package. The normal
path must be on `main`, have no tracked or staged changes, and exactly match
`origin/main`; untracked files do not block it. Explicit `development_mode=true`
keeps the clean-tree and exact pinned-HEAD checks but permits a named non-main
branch so DevMCP can install and test its own feature branch without first
merging it. The updater records the installed SHA/branch in operator config,
performs a user-level `uv tool install --force`, refreshes the user systemd units,
and then performs the MCP-health-before-tunnel restart sequence.


## Project selection boundary

Operator-configured `workspaces` are passed to the runtime as project discovery
roots. `list_projects` recursively discovers Git repositories without following
symlinks. A service-managed last-project file is read only as the initial default
for a new Runtime. Streamable HTTP `select_project` does not rewrite that global
file. Instead, each HTTP client lifecycle receives an opaque logical
`context_id`; selected project and default cwd are stored in the server-owned
logical context. Tool results expose the exact `workspace`/`active_project` used
by the call. If a connector opens a fresh MCP transport session for a later tool
call, passing the prior `context_id` resumes that same logical context without
making it globally visible to other clients. Explicit stale contexts fail rather
than silently falling back to another project's state.

Long-running HTTP commands use opaque `job_...` handles in a server-owned job
registry. A job is bound to its owning logical context, so a different client
context cannot poll, read, write, or cancel it. Running jobs survive transport
session teardown; completed jobs expire after a bounded retention interval.

Once selected, the repository is the primary writable root. Path trust is based
on canonical containment, not spelling: relative paths resolve from the logical
cwd; absolute paths and paths containing `..` are allowed when their canonical
target remains inside an authorized root. Sibling/ancestor escapes and symlink
escapes are rejected. Credential/runtime subtrees such as `.git`, `.ssh`,
`.aws`, `.config/gcloud`, `.env*`, private-key files, `.npmrc`, `.pypirc`, and
similar protected paths remain denied regardless of whether the input path was
relative or absolute.


## Permission and capability model

The internal execution model is `PLAN` and `BUILD`. PLAN is read-only. BUILD is the default and runs commands directly as the current OS user with normal host filesystem, HOME/PATH, environment, toolchains, temporary directories, and network access. The selected project is the default coding context/cwd, not a filesystem security boundary.

`permission_mode=safe|trusted|dangerous` is only an ingress compatibility adapter: `safe -> plan`, while `trusted` and `dangerous -> build`. There is no per-command ApprovalEngine, capability lease, network-target gate, or command deny-list evaluation in BUILD. High-level Git tools remain scoped to the selected repository and keep their correctness checks (explicit paths, branch/ref validation, CAS/state-drift checks, and force-push prohibition).

## Bounded Antigravity delegation

`antigravity_delegate` is a specialized fallback with its own worktree and validation constraints. It does not use the retired per-command capability/approval layer. The tool runs the host `agy` binary in a temporary
detached Git worktree at the selected project's current HEAD, not in the
operator's live checkout. The live checkout must have no tracked or staged
modifications before delegation; untracked files are not copied into the
temporary worktree.

The binary is discovered from `DEVMCP_ANTIGRAVITY_BIN`, the service `PATH`,
`~/.local/bin/agy`, or `/usr/local/bin/agy`, in that order. The configured or
discovered path must be an executable file.

The delegated prompt explicitly treats repository content and tool output as
untrusted data. The host wrapper independently rejects repositories with tracked
sensitive paths, uses a minimal environment, sets `PWD`/`OLDPWD` to the exact
workspace being launched, isolates AGY cache/state from the service's ambient
state, disables the real Git remote through per-process Git configuration, and
requires both explicit `--new-project` workspace binding and Antigravity's
sandbox capability. A pre-exec guard checks the actual process cwd before AGY
starts. The delegate may not commit,
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
rejects force push, withholds raw push output on failure, and verifies the authoritative remote branch head after push.

`git_fetch` always uses `--prune`; `git_pull` always uses `--ff-only` and refuses
to run with tracked or staged worktree changes. `git_merge_remote_branch` also
requires a clean tracked/staged worktree, accepts only a fetched branch from a
configured remote, and runs `git merge --abort` on failure. Successful fetch/pull/merge transitions refresh the state checkpoint; unexpected external mutations still fail with `STATE_DRIFT`. Local branch deletion uses only `git branch -d`, never `-D`. Remote branch deletion accepts only a configured remote.

The generic task registry intentionally does not advertise Git commands. High-level Git inspection and mutation tools remain explicitly scoped to the selected repository.

## Project-native verification

`project_checks` prefers repository-owned Makefile gates when present. For a
Python project, a usable project `.venv` has priority. If no `.venv` is usable
and `uv.lock` is present, the fallback test command is
`uv run --offline --frozen --no-sync python -m pytest`; it never silently falls
back to a host bare `pytest`. The resolved environment removes the isolated
DevMCP Runtime venv bin from PATH, then prefers project-local tooling and a
sanitized host PATH. `project_checks` reports the resolved interpreter/package
manager/PATH; `run_project_check` preflights its executable and reports missing
project dependencies explicitly. It executes only a discovered argv using the resolved PLAN/BUILD execution model and does not auto-install dependencies.

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

`apply_patch` accepts the standard envelope. Patch parsing, exact context matching, baseline/hash preconditions, atomic writes, rollback on multi-file failure, symlink defenses, and destructive-change thresholds remain correctness safeguards. They do not require retired approval IDs.

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

`exec_command` retains the compatibility shell-string surface and still accepts legacy `argv`. `exec_argv` is the preferred structured primitive. BUILD launches directly on the host as the current user; PLAN denies process execution. Registered tasks and project checks reuse the same process/session lifecycle.

Shell syntax such as `$()`, pipes, redirection, heredocs, `&&`, and inline scripts is ordinary BUILD behavior. BUILD has no DevMCP filesystem/network sandbox; OS-user permissions are the authority. PLAN does not execute shell commands.

Both `exec_command` and `exec_argv` default to non-transactional direct execution in BUILD. `transaction_mode: "apply"` remains an explicit compatibility path that creates a bounded snapshot, checks authoritative baselines, and applies a staged delta. Concurrent edits produce `TRANSACTION_CONFLICT`; no path uses `git reset --hard` or erases pre-existing dirty WIP.

The normal BUILD path is a direct subprocess of the current OS user. It does not layer bwrap, Landlock, a repository snapshot, or a specialized executor on ordinary development commands. Self-host identity is exposed through `local_state_snapshot`, `server_info`, and `service_status`.

`exec_command` and `write_stdin` default `yield_time_ms` to `10000` and honor
requested initial waits up to the schema maximum of 300 seconds. Short commands
return `status: "success"` for exit code 0 or `status: "failed"` for non-zero
exit. `command_success` is explicit (`true`/`false`, or `null` while running), so
`ok: true` cannot be mistaken for a passing check. A still-running HTTP command
returns an opaque `job_...` handle and a machine-readable `next_action` for
`write_stdin` with empty `chars`; the action includes its owning `context_id` so
it remains usable after a connector creates a fresh MCP transport session.

Only truncated terminal output returns a `read_output` next action by default.
`output_ref` values are `session:<id>:stdout` or `session:<id>:stderr`; offsets
are stream-specific absolute byte positions. HTTP shared jobs remain
owner-context checked across `job_status`, `job_output`, `job_input`,
`job_cancel`, `write_stdin`, `read_output`, and `kill_session`. Runtime limits
bound active commands, retained completed jobs/sessions, per-session output,
total output, and retention time.

Repository snapshots are not part of normal BUILD execution. They exist only for the explicit transactional compatibility path; completion, failure, timeout, cancellation, and runtime shutdown still clean owned snapshots safely.

Use `tty: true` only when a program requires a terminal. POSIX receives a real
PTY (`isatty()` is true). This build returns `TTY_UNSUPPORTED` on Windows rather
than labeling pipes as a TTY.

## Legacy permission-mode compatibility

`permission_mode=safe|trusted|dangerous` is a thin ingress-only compatibility adapter for the execution model: `safe` maps to `plan` (read-only), while `trusted` and `dangerous` map to `build` (full-access). Mode resolution happens once at startup by `resolve_execution_mode()`. There is no runtime policy profile gate or in-process approval decision applied after resolution.

`activate_policy_profile`, `grant_root`, `grant_capability`, and `end_task_scope`
are retired as of v0.1.0b1 and no longer advertised in `tools/list`.

`--dangerously-fake-readonly-annotations` is a fenced test/debug compatibility
switch that advertises every tool as read-only in `tools/list`. It does not stop
mutation or execution and is never recommended to avoid client prompts.
`server_info` and the server card keep reporting the real annotations. See
[permission-modes.md](permission-modes.md).
