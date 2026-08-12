# Tools And Schemas

The normative behavior is [runtime-contract-v0.2.md](runtime-contract-v0.2.md).
Live JSON Schemas come from `tools/list`; CI compares their names, input
properties, annotations, and error codes with the contract.

The server metadata exposes tool schema version `1.0`. Additive optional fields
are preferred. A breaking tool name, required-input, or annotation change must
be documented as requiring a ChatGPT Developer Mode app **Refresh** while the
app remains a draft.

## Fixed inventory

The default catalog contains exactly 62 tools:

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
- `code_diagnostics`: Normalize compiler/traceback diagnostics without requiring an IDE/LSP dependency.
- `grant_root`: Grant one operator-authorized directory as a temporary read/write root.
- `grant_capability`: Grant one narrow, expiring capability target.
- `list_capability_leases`: List temporary capability leases owned by the current logical context.
- `revoke_capability_lease`: Revoke one owned temporary capability lease.
- `end_task_scope`: End one logical task scope and revoke its task-scoped leases.
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
- `approval_status`: Approval status.
- `list_pending_approvals`: List pending approvals.
- `check_exec_environment`: Check exec environment.
- `get_default_cwd`: Get default cwd.
- `set_default_cwd`: Set default cwd.
`view_image` may be disabled when an installation cannot accept binary image
content. That capability gate is not a tool profile. The other 61 tools are
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

`grant_root` adds an existing directory below an operator-configured
`DEVMCP_GRANTABLE_ROOTS` ceiling as a temporary read or write root. The grant is
an in-memory capability lease scoped to one operation, logical task, or logical
session; it never survives a restart. Project discovery roots do not implicitly
populate this ceiling; unset `DEVMCP_GRANTABLE_ROOTS` means no additional roots
can be granted. Granting an ancestor of the primary
workspace is rejected as too broad. Filesystem reads, writes, patching, command
path checks, and execution snapshots use the same canonical root set. Additional
roots are copied through the same secret-filtering snapshot path and are mounted
at their canonical paths; host sibling directories are not directly bind-mounted
into model-controlled commands.

## Permission and capability model

The runtime has one authoritative user-facing decision matrix: policy profiles
(`safe`, `balanced`, `power`, `autonomous`, `custom`) resolve every named
capability to `auto`, `ask`, or `deny` at Runtime startup. Legacy
`permission_mode=safe|trusted|dangerous` remains only as a compatibility adapter
to `safe|power|autonomous`; low-level execution does not consult a second legacy
permission matrix after initialization. `server_info` and
`check_exec_environment` expose the effective capability decisions.

The policy matrix is separate from an immutable host-security floor. Profiles
and leases cannot authorize host Docker/Podman control, privilege escalation,
arbitrary host filesystem access, protected credential paths, model-supplied
sandbox-attestation state, or an unenforced network target. Ambient host secrets
are always filtered. A specific host secret is injected only when the command
names it in `sensitive_env_names` and the current context owns an exact-name
`env.sensitive` lease.

`grant_capability` creates an opaque `lease_...` record for a bounded capability
and target. Supported targets include executable/command patterns, dependency
installation, exact sensitive environment names, network scope, and workspace
create/delete/move/patch operations. Leases have TTLs and `once`, `task`, or
`session` scope. One-shot leases are consumed after the first public tool
operation that uses them, not halfway through internal preflight. Task-scoped
leases use an opaque `task_scope_id`; `end_task_scope` revokes all leases for
that task immediately. The model cannot create a permanent grant.

The local namespace sandbox cannot truthfully enforce `github.com`-only egress,
so destination-scoped network leases fail with `CAPABILITY_UNAVAILABLE` unless
an operator-configured executor with real network-target filtering is available.
Broad network still follows the normal `network.public`/`network.host_local`
capability decision.

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
Python project, a usable project `.venv` has priority. If no `.venv` is usable
and `uv.lock` is present, the fallback test command is
`uv run --offline --frozen --no-sync python -m pytest`; it never silently falls
back to a host bare `pytest`. The resolved environment removes the isolated
DevMCP Runtime venv bin from PATH, then prefers project-local tooling and a
sanitized host PATH. `project_checks` reports the resolved interpreter/package
manager/PATH; `run_project_check` preflights its executable and reports missing
project dependencies explicitly. It executes only a discovered argv in the
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

`exec_command` retains the compatibility shell-string surface and still accepts
legacy `argv`. `exec_argv` is the preferred first-class structured execution
primitive. It passes the argument vector without shell parsing while retaining
the same capability, root, sandbox, network, secret-environment, and approval
enforcement. Registered tasks remain a fast path for common known-safe commands,
not a requirement or an ecosystem whitelist.

Shell syntax is not itself the security boundary. Top-level pipeline,
conditional, and sequence segments are classified independently for policy/risk
signals, but `$()`, pipes, redirection, heredocs, `&&`, and ordinary inline
scripts are usable when the effective capability policy permits execution. The
namespace/root/network boundary enforces effects. Privilege escalation, direct
host Docker/Podman control, protected credentials, and workspace/root escape
remain hard-denied. Bubblewrap drops all capabilities and disables creation of
nested user namespaces in model-controlled processes.

Inside `bwrap`, `/tmp` is a writable private tmpfs and `TMPDIR`, `TMP`, and
`TEMP` all point to `/tmp`; host `/tmp` is not the command's writable namespace.
The read-only system view is declarative rather than a whole `/etc` bind: normal
OS/linker/toolchain metadata is mounted read-only, CA/DNS metadata is added when
network is granted, and sensitive account/credential files such as
`/etc/shadow`, SSH, cloud, and package-registry credentials are excluded.

`exec_argv` defaults to `transaction_mode: "apply"` on the local secure
namespace backend. Commands run against filtered snapshots mounted at canonical
root paths. On exit 0 the runtime calculates the actual changed files, evaluates
their create/update/delete capabilities, re-checks each authoritative baseline,
and atomically applies the bounded staged set. Non-zero exit/timeout discards the
snapshot. Concurrent edits produce `TRANSACTION_CONFLICT`; the runtime never
uses `git reset --hard` or replaces a user's pre-existing dirty state with HEAD.
`exec_command` retains `transaction_mode: "discard"` by default for backward
compatibility; callers may opt in to transactional apply.

Execution goes through an internal backend scheduler. `local_sandbox` is the
default secure backend; an attested `inherited_sandbox` avoids unsupported nested
user namespaces during DevMCP self-hosting. `ephemeral_container` is optional
and exists only when the operator configures an absolute trusted runner in
`DEVMCP_EPHEMERAL_CONTAINER_RUNNER`. The runner receives a bounded manifest and
runtime-owned filtered snapshots, never a model-visible host Docker/Podman socket
or direct writable workspace mount. CPU, memory, PID, time, network, and mount
requirements are explicit in the runner protocol, and extracted changes still
pass transaction/baseline checks. Missing/insufficient backends fail once with
`CAPABILITY_UNAVAILABLE` instead of producing a series of opaque command errors.

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

## Legacy permission-mode compatibility

`permission_mode=safe|trusted|dangerous` is no longer a second live permission
engine. At startup it maps to policy profile `safe|power|autonomous` when an
explicit profile was not supplied. After initialization the Runtime consults
only the effective capability matrix plus the immutable host-security floor.
Legacy `dangerous` therefore means autonomous policy decisions; it does **not**
disable secret filtering, root confinement, protected paths, sandbox attestation,
or the Docker/privilege hard boundary. These compatibility names do not change
the tool list.

`activate_policy_profile` is idempotent: requesting the already-effective
profile returns `status: "unchanged"` and does not schedule a service restart.

`--dangerously-fake-readonly-annotations` is a fenced test/debug compatibility
switch that advertises every tool as read-only in `tools/list`. It does not stop
mutation or execution and is never recommended to avoid client prompts.
`server_info` and the server card keep reporting the real annotations. See
[permission-modes.md](permission-modes.md).
