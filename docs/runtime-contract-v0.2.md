# Coding Tools MCP Runtime Contract v0.2

Status: implemented contract for `coding-tools-mcp` 0.2.x.

Protocol target: MCP `2025-11-25`, with explicit compatibility for `2025-06-18`.

This contract describes one stable, model-neutral coding tool set. There are no
tool profiles and the server does not add or remove process tools dynamically.
`apply_patch` is the only direct file-mutation primitive; `edit_file` is not
provided. DevMCP operates in two execution modes: PLAN (read-only) and BUILD (full-access). Legacy --permission-mode flags map at ingress: safe -> plan, trusted/dangerous -> build.

One switch, `--dangerously-fake-readonly-annotations`, rewrites the exposure hints
in `tools/list` for test/debug compatibility with clients that gate on mutating
annotations. It is not a recommended way to avoid client prompts: the catalog,
the schemas, and what every tool actually does are all unchanged, and no tool is
hidden. It requires `dangerous` permission mode, requires authentication over
HTTP, and is reported by `server_info.annotation_override` and the server card,
both of which continue to publish the real annotations recorded below. Unless
that switch is set, the annotations in this document are what `tools/list`
returns.

## Protocol and transports

- Streamable HTTP uses `POST /mcp`. `DELETE /mcp` terminates the selected
  `Mcp-Session-Id`. Because this server does not provide an SSE stream,
  `GET /mcp` and `HEAD /mcp` return `405`.
- `/healthz` and `/readyz` include bounded `http_sessions` capacity telemetry:
  capacity, total records, sessions/requests currently active, records being
  created, and records closing. Session identifiers are never exposed there.
- Each successful HTTP `initialize` creates an independent runtime. Its cwd,
  process sessions, retained output, and runtime directories are not shared
  with other MCP sessions.
- Subsequent HTTP messages must include the returned `Mcp-Session-Id` and the
  negotiated `MCP-Protocol-Version`. Unknown or expired sessions return `404`.
- JSON-RPC batches are rejected. Cancellation uses
  `notifications/cancelled.params.requestId`.
- stdio is newline-delimited JSON-RPC. stdout contains protocol messages only;
  diagnostics and logs go to stderr.
- The only advertised server capability is stable tools with
  `listChanged: false`. Logging, resources, prompts, sampling, and elicitation
  are not advertised.

The server accepts only the protocol versions listed above. A supported version
is echoed in `initialize`; arbitrary older dates and unknown future dates are
rejected rather than compared lexicographically.

## Automatic project context and session project selection

Initialization automatically loads bounded root project instructions from
`AGENTS.md`, `AGENTS.MD`, `CLAUDE.md`, and `CLAUDE.MD` when present. The content
is included in the MCP `instructions` field, so an agent does not need an
`open_workspace` call. Nested instruction files are indexed by path but are not
eagerly injected. Loading is UTF-8 safe and bounded by file-count, scan-count,
depth, per-file, and total-byte limits.

The operator may configure one or more project-library roots. The runtime
recursively discovers Git repositories below those roots without following
symlinks. A configured `DEVMCP_ACTIVE_PROJECT_FILE` is an initial-default hint
only: a new Runtime may load it at construction, but Streamable HTTP client
selection never rewrites it. This prevents one chat/client from changing the
workspace subsequently observed by another.

Streamable HTTP uses a server-owned logical-context registry above transport
`Mcp-Session-Id`. Initialization creates an opaque `context_id`; every tool
result exposes that capability together with the exact `workspace` and
`active_project` used by the call. Calls within one transport session reuse its
context automatically. If a client reconnects or a connector creates a new MCP
transport session between tool calls, it can pass the previous `context_id` to
continue the same logical project/default-cwd state. Contexts are isolated from
one another, guarded by a per-context lock, and expire after an idle TTL (3600s
by default, configurable with `DEVMCP_LOGICAL_CONTEXT_TTL_SECONDS`). An explicit
expired context fails with `CONTEXT_NOT_FOUND`; an already-live transport that
does not explicitly present the stale capability rolls to a new context from
its own current state. Treat `context_id` as a bearer capability and never share
it between clients/chats.

Long-running HTTP commands use a separate server-owned job registry. Returned
`job_...` handles are opaque and bound to the logical `context_id`; a different
context receives `ACCESS_DENIED` even if it obtains the job handle. Running jobs
survive individual MCP transport Runtime teardown. Completed jobs are retained
for bounded output/status access for 300s by default
(`DEVMCP_COMPLETED_JOB_TTL_SECONDS`), after which the handle returns
`status: "not_found"`. Timeout, explicit cancellation, server shutdown, and
registry cleanup terminate/release owned resources.

## Workspace and patch guarantees

- One MCP-session runtime owns one canonical active repository root at a time.
- Path authorization is canonical-root based rather than syntax based. Relative
  paths resolve from logical cwd; absolute paths and inputs containing `..` are
  accepted only when their canonical targets remain inside an authorized root.
  NUL bytes, sibling/ancestor escapes, symlink escapes, and protected
  credential/runtime paths remain rejected.
- The selected project is the primary root. Path validation (workspace boundary,
  symlink rejection, NUL/traversal rejection) applies to all file tools.
  Normal shell execution does not create a repository snapshot.
- `apply_patch` parses and validates every operation before committing.
- Every replacement is prepared and fsynced in the target directory, then
  installed with `os.replace`.
- Existing mode bits, UTF-8 BOMs, and CRLF/LF style are preserved. Moves inherit
  the source mode.
- Baseline hashes and modes are checked before commit and again immediately
  before replacement. Conflicts are retryable and never silently overwrite a
  newly-created target.
- A failed multi-file commit restores all backups. Portable filesystems do not
  offer a true transaction across directories, so a rollback failure is
  reported explicitly as `PATCH_ROLLBACK_FAILED` with recovery details.

## Result contract

Every valid `tools/call` response contains:

```json
{
  "content": [{"type": "text", "text": "Short agent-readable result"}],
  "structuredContent": {"ok": true},
  "isError": false
}
```

`content` is concise model-facing text and is never a JSON serialization of the
whole payload. Its normal size is governed by each tool's own per-call limits
(`max_bytes`, `max_output_bytes`, `max_results`, ...), without the former
16 KiB renderer preview cap. A 2,162,688-byte emergency safety ceiling protects
clients from pathological individual entries that count-based limits cannot
bound. Command results always begin with a status line (status, exit code,
signal, timeout). Stable pageable truncation names an executable continuation
call (`read_output(output_ref=..., offset=...)`,
`read_file(path=..., start_line=...)`, ...); non-pageable results explicitly
say which limit or scope to change. `structuredContent` is the complete,
stable machine-readable interface. Large diffs and command output are not
copied into `_meta`; `_meta` is optional UI extension space only.

Tool failures keep the same envelope with `isError: true`, a readable error in
`content`, and this machine shape:

```json
{
  "ok": false,
  "error": {
    "code": "PATCH_CONTEXT_AMBIGUOUS",
    "message": "Patch context matched more than one location.",
    "category": "validation",
    "retryable": true,
    "details": {"path": "src/app.py", "hunk_index": 0, "match_count": 2}
  }
}
```

Known tool error codes include:

```json
["ABSOLUTE_PATH_DENIED", "ACCESS_DENIED", "AGENT_TASK_FAILED", "BINARY_FILE", "CAPABILITY_UNAVAILABLE", "CONTEXT_INVALID", "CONTEXT_NOT_FOUND", "EXECUTION_DENIED", "EXECUTOR_FAILED", "EXECUTOR_PROTOCOL_ERROR", "GIT_CONFLICT", "GIT_ERROR", "GIT_NOT_FOUND", "HOST_CLI_PROBE_FAILED", "INTERNAL_ERROR", "INVALID_ARGUMENT", "INVALID_STATE", "IS_DIRECTORY", "NOT_A_DIRECTORY", "NOT_FOUND", "NOT_IMPLEMENTED", "OUTPUT_TOO_LARGE", "PATCH_BASELINE_LIMIT", "PATCH_CONFLICT", "PATCH_CONTEXT_AMBIGUOUS", "PATCH_CONTEXT_NOT_FOUND", "PATCH_FAILED", "PATCH_HUNKS_OVERLAP", "PATCH_ROLLBACK_FAILED", "PATH_OUTSIDE_WORKSPACE", "PERMISSION_REQUIRED", "PROJECT_ENVIRONMENT_ERROR", "REMOTE_HEAD_MISMATCH", "RUNTIME_DIR_UNWRITABLE", "SANDBOX_FAILED", "SANDBOX_UNAVAILABLE", "SERVICE_COMMAND_FAILED", "SERVICE_UNAVAILABLE", "SESSION_CLOSED", "SESSION_LIMIT_REACHED", "SESSION_NOT_FOUND", "STATE_DRIFT", "SYMLINK_ESCAPE", "TIMEOUT", "TRANSACTION_CONFLICT", "TRANSACTION_SNAPSHOT_FAILED", "TRANSACTION_TOO_LARGE", "TRANSACTION_UNSAFE_CHANGE", "TTY_UNSUPPORTED", "UNSUPPORTED_ENCODING", "WRITER_LEASE_CONFLICT"]
```

Error categories are `validation`, `security`, `permission`, `runtime`,
`not_found`, `conflict`, and `internal`.

Malformed JSON-RPC uses standard protocol errors: parse `-32700`, invalid
request `-32600`, unknown method `-32601`, invalid params/tool `-32602`, and
unexpected server failure `-32603`.

## Process lifecycle

`exec_command`, `write_stdin`, `read_output`, and `kill_session` are always in
the catalog. `exec_command` and `write_stdin` default to a 10-second yield.
Initial execution honors requested `yield_time_ms` values up to the schema
maximum of 300 seconds instead of silently capping them at 30 seconds, allowing
long project checks to complete in one tool call when a client does not retain
the same MCP runtime for polling. A short command normally finishes in one call.
A running command returns:

```json
{
  "status": "running",
  "session_id": "...",
  "next_action": {
    "tool": "write_stdin",
    "arguments": {"session_id": "...", "chars": "", "yield_time_ms": 10000}
  }
}
```

Call `write_stdin` with empty `chars` to poll. `read_output` is needed only when
output is truncated or a caller explicitly requested compact retained output.
Its offsets are absolute and independent for stdout and stderr. A single
truncated stream is selected by `next_action`; when both streams are truncated,
`next_actions` contains one executable `read_output` call for each stream.

Active processes, completed-output sessions, per-session bytes, and total
runtime bytes are bounded. Completed sessions have a TTL. POSIX `tty=true` uses
a real pseudo-terminal; Windows reports `TTY_UNSUPPORTED` in this build instead
of pretending pipes are a TTY.

Normal non-transactional execution never creates a repository-sized snapshot.
`read-only` executes against the authoritative workspace mounted read-only by
bwrap on Linux, with a private writable `/tmp`. `workspace-write` executes
against the authoritative workspace mounted read-write by bwrap and uses the
project's real environment/toolchain. Those normal bwrap paths do not add a
second Landlock filesystem policy. `full-access` executes directly as the
current user without bwrap, Landlock, or a workspace snapshot; it inherits the
normal host filesystem, environment, temporary directories, network, and
available user-level container tooling. `full-access` does not imply root or
privilege escalation, and `sudo`/`su`/`doas` remain rejected.

Repository snapshots remain only for explicit `transaction_mode="apply"`
compatibility execution. Those owned snapshots still carry runtime ownership
markers and are synchronously cleaned on normal exit, command failure, timeout,
cancellation, startup failure, or runtime shutdown. Transaction cleanup verifies
the canonical owned path before removal and cannot delete the selected real
workspace.

## HTTP authentication

Non-loopback deployment requires bearer or OAuth authentication unless the
operator explicitly selects no-auth. OAuth implements Authorization Code +
PKCE S256, protected-resource metadata, authorization-server metadata, exact
redirect URI matching, one-time five-minute codes, 24-hour access tokens, and
RFC 7591 dynamic client registration at `POST /oauth/register`. Public and
confidential clients are bound to their registered authentication method.

Dynamic registrations and authorization codes are process-local; restarting
the server requires clients to register again. Configure a stable
`CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET` and public server URL only when tokens must
survive tunnel churn. Forwarded headers are ignored unless
`CODING_TOOLS_MCP_TRUST_PROXY_HEADERS=1` is explicitly set.

## Stable tool inventory

The default catalog has 55 tools, including `view_image`. Setting
`CODING_TOOLS_MCP_ENABLE_VIEW_IMAGE=0` is the sole installation capability gate
and removes only that optional binary-content tool. It is not a tool profile.

Each definition below lists the live input property names and annotations. The
authoritative JSON Schemas are returned by `tools/list` and checked for drift in
CI. The annotations recorded here are the truthful ones and are what `server_info`
and the server card always report, including while
`--dangerously-fake-readonly-annotations` is rewriting the hints in `tools/list`.

### server_info

Inputs: none.

Annotations: `{"title":"Server info","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns server version, protocol, workspace, cwd, fixed tool count, auth state,
permission mode, runtime directories, project-context metadata, exec policy,
and the auto-allow/approval/deny permission policy.

### service_status

Inputs: none.

Annotations: `{"title":"DevMCP service status","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Runs the fixed host-side `devmcp status` diagnostic outside the execution
sandbox and returns its bounded stdout/stderr and exit code.

### service_doctor

Inputs: none.

Annotations: `{"title":"DevMCP service doctor","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Runs the fixed host-side `devmcp doctor` diagnostic outside the execution
sandbox and returns its bounded stdout/stderr and exit code.

### host_cli_probe

Inputs: `"path"`, `"probe"`.

Annotations: `{"title":"Probe host CLI capability","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Resolves one executable file inside the selected project and performs only a
bounded capability probe. `probe=path` returns the resolved executable without
running it; `probe=version` runs only `--version`; `probe=help` runs only
`--help`. Host execution uses the selected project root as cwd, a small
sanitized environment allowlist, a 30-second timeout, and bounded stdout/stderr.
Arbitrary argv and arbitrary environment injection are not exposed.

### service_restart

Inputs: `"approval_id"`.

Annotations: `{"title":"Restart DevMCP services","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Controlled by `service.manage`. Schedules a delayed trusted `devmcp restart` in
a separate user-systemd transient unit so the current tool response can complete
before the serving process is replaced. The CLI restarts MCP first, waits for a
successful MCP health probe, then restarts the tunnel if its unit is installed.

### service_update

Inputs: `"source_project"`, `"development_mode"`, `"approval_id"`.

Annotations: `{"title":"Update DevMCP service runtime","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Controlled by `service.manage`. Resolves exactly one discovered local Git
checkout whose `pyproject.toml` declares `project.name = "devmcp-runtime"`.
The normal source must be on `main`, have no tracked or staged changes, and
satisfy `HEAD == origin/main`; untracked files do not block the update. Explicit
`development_mode=true` still requires a clean named branch and pins the exact
40-character source HEAD, but permits a non-main branch for self-host testing.
The trusted CLI revalidates source path, branch, cleanliness, and expected SHA,
records the installed SHA/branch in operator config, runs a user-level
`uv tool install --force`, reinstalls the user systemd units using the newly
installed runtime, and performs the same MCP-health-before-tunnel restart
sequence as `service_restart`. No sudo or system-level package/service mutation
is used.

### check_exec_environment

Inputs: none.

Annotations: `{"title":"Check exec environment","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns lightweight policy and Landlock status without running active probes.

### get_default_cwd

Inputs: none.

Annotations: `{"title":"Get default cwd","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### set_default_cwd

Inputs: `"path"`.

Annotations: `{"title":"Set default cwd","readOnlyHint":false,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Changes only the active logical context's navigation base; it does not modify
files. HTTP reconnects retain it when the same `context_id` is supplied.

### list_projects

Inputs: none.

Annotations: `{"title":"List projects","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Discovers Git repositories under operator-approved project roots. Discovery is
bounded, canonicalized, does not follow symlinks, and does not change the active
project.

### select_project

Inputs: `"project"`.

Annotations: `{"title":"Select project","readOnlyHint":false,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Selects exactly one discovered repository. Running command sessions prevent
switching. Existing execution sandboxes are discarded, the new repository
becomes the file/patch/exec/Git/delegation root, and project context is reloaded.
For Streamable HTTP, the selected project is saved in the logical context only;
`DEVMCP_ACTIVE_PROJECT_FILE` is not rewritten. Direct/stdio Runtime instances may
retain the legacy persisted-last-project behavior when explicitly configured.
No configured project root is added or changed.

### current_project

Inputs: none.

Annotations: `{"title":"Current project","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns the selected repository, its operator root, relative path, and known
root authority files.

### local_state_snapshot

Inputs: none.

Annotations: `{"title":"Local state snapshot","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns project, branch, HEAD, upstream/ahead/behind state, dirty/staged paths,
runtime version and installed service SHA, plus self-host diagnostics including
source checkout SHA, installed/source match, nested-sandbox state, default
execution mode, and any compatibility policy profile in one MCP response.

### project_checks

Inputs: none.

Annotations: `{"title":"Project checks","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns bounded project-native verification argv plus the resolved execution
environment: selected workspace, project `.venv`, interpreter, detected package
manager, sanitized PATH, runtime Python, and environment warnings. Repository
Makefile targets are preferred. When DevMCP supplies Python fallback discovery,
a project `.venv` has priority over package-manager fallback; uv fallback uses
`uv run --offline --frozen --no-sync` and never silently chooses bare host
`pytest`.

### run_project_check

Inputs: `"check_id"`, `"timeout_ms"`, `"yield_time_ms"`, `"max_output_bytes"`, `"approval_id"`.

Annotations: `{"title":"Run project check","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Runs only an argv returned by `project_checks`, inside the selected repository's
normal execution sandbox. It does not automatically install dependencies or
grant network access. Project-native PATH resolution removes an isolated DevMCP
Runtime venv bin from PATH instead of shadowing the project's `python`,
`python3`, `pytest`, `ruff`, etc.; project `.venv/bin` is preferred when present.
The result reports the resolved execution environment. A missing check
executable fails preflight with `PROJECT_ENVIRONMENT_ERROR`; common missing
module/tool output is classified as `PROJECT_DEPENDENCY_MISSING`.

### run_checks_for_diff

Inputs: `"timeout_ms"`, `"max_output_bytes"`, `"max_checks"`, `"approval_id"`.

Annotations: `{"title":"Run checks for diff","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Reads the current Git dirty paths, selects relevant discovered project checks
for the changed file types, runs them synchronously server-side with bounded
output, and returns one aggregate result. It never installs dependencies or
invents commands not exposed by `project_checks`.

### read_file

Inputs: `"path"`, `"start_line"`, `"end_line"`, `"max_lines"`, `"max_bytes"`, `"encoding"`.

Annotations: `{"title":"Read file","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Reads UTF-8 ranges as a stream, reports full file line/byte metadata, rejects
binary content, and returns continuation metadata when bounded.

### read_files

Inputs: `"paths"`, `"per_file_max_bytes"`, `"per_file_max_lines"`, `"total_max_bytes"`.

Annotations: `{"title":"Read files","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Each `paths` entry may be a string or an object with `path`, optional line range,
and per-entry bounds. The batch returns per-file `ok`/structured errors,
truncation metadata, partial-success counts, and a total response budget instead
of failing the whole call when one file is missing/binary/unauthorized.

### code_diagnostics

Inputs: `"text"`, `"provider"`, `"source"`, `"max_results"`.

Annotations: `{"title":"Code diagnostics","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Normalizes compiler/traceback-style text through an optional provider registry.
The built-in `compiler-text` provider extracts path/line/column/severity without
making an IDE or language server a core dependency. Reported paths are checked
against the same authorized root resolver and annotated as authorized or
outside-root; diagnostics themselves never grant filesystem authority.

### list_dir

Inputs: `"path"`, `"recursive"`, `"max_depth"`, `"max_entries"`, `"include_hidden"`, `"include_ignored"`, `"sort"`.

Annotations: `{"title":"List directory","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### list_files

Inputs: `"path"`, `"patterns"`, `"glob"`, `"exclude_patterns"`, `"include_hidden"`, `"include_ignored"`, `"max_results"`, `"sort"`.

Annotations: `{"title":"List files","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Traversal is iterative and git-ignore checks are batched.

### search_text

Inputs: `"query"`, `"path"`, `"is_regex"`, `"case_sensitive"`, `"glob"`, `"context_lines"`, `"max_results"`.

Annotations: `{"title":"Search text","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Ripgrep output is consumed incrementally and the process stops once the result
cap is known to be exceeded. `context_lines=0` does not reread matching files.

### inspect_symbol

Inputs: `"symbol"`, `"path"`, `"context_lines"`, `"max_results"`.

Annotations: `{"title":"Inspect symbol","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Performs one bounded whole-word search, classifies common declaration forms as
definitions, separates other references and test-file matches, and returns
bounded surrounding source. It is a lightweight cross-language inspection
primitive rather than a language-server replacement.

### apply_patch

Inputs: `"patch"`, `"dry_run"`, `"approval_id"`.

Annotations: `{"title":"Apply patch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Supports Add/Update/Delete/Move operations inside a `*** Begin Patch` /
`*** End Patch` envelope. Canonical path validation, baseline hashes/modes, and
atomic replacement remain mandatory. In normal `workspace-write`, create,
update, and move apply directly to the authoritative workspace when their
preconditions still match; there is no whole-repository transaction snapshot.
Delete and destructive-threshold patches retain explicit confirmation. Safe and
explicit compatibility-policy profiles may impose stricter approval behavior.
Preview returns a unified diff, line counts, removal percentage, and risk
classification.

### exec_command

Inputs: `"cmd"`, `"argv"`, `"cwd"`, `"workdir"`, `"timeout_ms"`, `"yield_time_ms"`, `"env"`, `"sensitive_env_names"`, `"transaction_mode"`, `"execution_mode"`, `"executor_backend"`, `"max_bytes"`, `"max_output_bytes"`, `"preview_bytes"`, `"tty"`, `"stdin"`, `"verbosity"`, `"network_required"`, `"network_targets"`, `"task_id"`, `"approval_id"`.

Annotations: `{"title":"Exec command","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Command execution separates MCP/tool transport success from subprocess success.
Completed commands use `status: "success"` for exit code 0 and
`status: "failed"` for non-zero exits. `command_success` is `true`, `false`, or
`null` while still running. Other states include `running`, `timeout`, and
`terminated`. Launch/policy failures still use the error envelope. `ok: true`
therefore means the tool call itself completed structurally; callers must use
`command_success`/`status` for command outcome.

Exactly one of `cmd` or `argv` is required. `cmd` retains the shell-string path
for compatibility and defaults to non-transactional execution. The default
execution mode follows the legacy compatibility mapping: `safe -> read-only`,
`trusted -> workspace-write`, `dangerous -> full-access`. Normal read-only and
workspace-write use the authoritative working tree through one bwrap filesystem
boundary on Linux; normal workspace-write exposes the real project `.venv`,
PATH/toolchain, and network rather than capability-by-capability approvals.
`full-access` directly inherits the current user's environment, filesystem,
temporary directories, and network, including sensitive environment values that
the service itself inherited. Legacy `argv` remains accepted here for
compatibility, but new callers should prefer `exec_argv`.

`transaction_mode="apply"` is explicit compatibility opt-in and is the only
normal shell path that builds a repository snapshot/transaction. Explicit
policy profiles continue to use the older approval/capability machinery as a
compatibility layer. `network_targets` and `executor_backend` remain public
compatibility inputs for those policy-managed/specialized executor paths; they
are not additional permission layers around the normal real-workspace fast path.

### exec_argv

Inputs: `"argv"`, `"cwd"`, `"workdir"`, `"timeout_ms"`, `"yield_time_ms"`, `"env"`, `"sensitive_env_names"`, `"transaction_mode"`, `"execution_mode"`, `"executor_backend"`, `"max_output_bytes"`, `"tty"`, `"stdin"`, `"verbosity"`, `"preview_bytes"`, `"network_required"`, `"network_targets"`, `"task_id"`, `"approval_id"`.

Annotations: `{"title":"Exec argv","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Preferred structured developer execution primitive. It never invokes a shell to
interpret `argv` and has the same three execution modes and non-transactional
default as `exec_command`. `transaction_mode="apply"` remains available as an
explicit bounded snapshot/commit compatibility path; baseline conflicts fail
with `TRANSACTION_CONFLICT` rather than overwriting concurrent/user WIP. No
rollback path uses `git reset --hard`.

### run_task

Inputs: `"task_id"`, `"args"`, `"path"`, `"cwd"`, `"env"`, `"timeout_ms"`, `"yield_time_ms"`, `"max_output_bytes"`, `"approval_id"`.

Annotations: `{"title":"Run task","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Tasks use validated registry metadata and an argv-only subprocess path. In the
normal compatibility modes they inherit the same read-only/workspace-write/
full-access execution view as shell commands, so trusted development checks see
the real project environment. Explicit policy profiles retain their policy
authorization behavior.

### write_stdin

Inputs: `"session_id"`, `"chars"`, `"yield_time_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`.

Annotations: `{"title":"Write stdin","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Poll or interact with a command session. Pass empty `chars` to wait for output.
For HTTP shared jobs, use the owning `context_id`; generated `next_action`
objects include it automatically.

### kill_session

Inputs: `"session_id"`, `"signal"`, `"wait_ms"`, `"kill_wait_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`.

Annotations: `{"title":"Kill session","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Statuses are `["terminated", "killed", "exited", "terminating", "not_found"]`.

### read_output

Inputs: `"output_ref"`, `"stream"`, `"offset"`, `"limit"`.

Annotations: `{"title":"Read output","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_status

Inputs: `"path"`, `"include_untracked"`, `"max_entries"`.

Annotations: `{"title":"Git status","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_diff

Inputs: `"path"`, `"staged"`, `"context_lines"`, `"max_bytes"`.

Annotations: `{"title":"Git diff","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_log

Inputs: `"path"`, `"ref"`, `"max_count"`, `"skip"`.

Annotations: `{"title":"Git log","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_show

Inputs: `"rev"`, `"path"`, `"context"`, `"max_bytes"`, `"include_diff"`.

Annotations: `{"title":"Git show","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_blame

Inputs: `"path"`, `"rev"`, `"start_line"`, `"end_line"`, `"max_lines"`.

Annotations: `{"title":"Git blame","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_create_branch

Inputs: `"name"`, `"approval_id"`.

Annotations: `{"title":"Create Git branch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Creates and switches to a validated local branch in the selected repository.
The `git.branch` policy capability is authoritative.

### git_switch_branch

Inputs: `"name"`, `"approval_id"`.

Annotations: `{"title":"Switch Git branch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Switches only to an existing validated local branch in the selected repository.

### git_fetch

Inputs: `"remote"`, `"approval_id"`.

Annotations: `{"title":"Fetch Git remote","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Fetches one configured remote using `--prune`. Arbitrary remote URLs are not
accepted. Controlled by `git.sync`; failed fetch output is withheld to avoid
credential disclosure.

### git_pull

Inputs: `"remote"`, `"approval_id"`.

Annotations: `{"title":"Pull Git branch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Pulls the current branch from one configured remote using `--ff-only` only.
Tracked or staged worktree changes cause `INVALID_STATE` before network access.
Controlled by `git.sync`.

### git_merge_remote_branch

Inputs: `"remote"`, `"branch"`, `"approval_id"`.

Annotations: `{"title":"Merge remote Git branch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Merges one already-fetched branch from a configured remote into the current
branch. Tracked or staged worktree changes are rejected before mutation. The
remote and branch names are validated, arbitrary URLs/options are not accepted,
and a failed merge is immediately followed by `git merge --abort` before the
tool returns `GIT_CONFLICT`, restoring the pre-merge branch state. Controlled by
`git.sync`.

### git_delete_branch

Inputs: `"name"`, `"approval_id"`.

Annotations: `{"title":"Delete local Git branch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Deletes only a non-current validated local branch using `git branch -d`; force
deletion is not exposed. Controlled by `git.branch`.

### git_delete_remote_branch

Inputs: `"name"`, `"remote"`, `"approval_id"`.

Annotations: `{"title":"Delete remote Git branch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Deletes one validated branch from a configured remote. Arbitrary remote URLs are
rejected. Controlled by `git.push`; failure output is withheld to avoid
credential disclosure.

### git_commit

Inputs: `"message"`, `"paths"`, `"approval_id"`.

Annotations: `{"title":"Git commit","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Requires an explicit bounded path list, rejects unrelated pre-staged paths,
stages only the named paths, and never runs `git add -A`. Returns branch, commit
SHA, and committed path set.

### git_push

Inputs: `"remote"`, `"force"`, `"approval_id"`.

Annotations: `{"title":"Git push","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Pushes the current branch only to a configured remote name, normally `origin`.
Arbitrary URL remotes and force push are rejected. `git.push` is controlled by
the active policy profile, and failed push output is withheld to avoid credential
disclosure.

### wait_for_external

Inputs: `"seconds"`, `"timeout_seconds"`.

Annotations: `{"title":"Wait for external process","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Waits without polling or mutating an external system. `seconds` may request 1-3600
seconds, while `timeout_seconds` is a hard 1-90 second bound for one tool call.
The result is `completed` when the requested wait fits inside the bound, `timeout`
when the hard bound expires first, or `cancelled` when the owning MCP request or
runtime is cancelled. Clients must then re-poll the authoritative external
connector rather than treating the wait result as remote-system state.

### continuation_checkpoint

Inputs: `"action"`, `"logical_task"`, `"branch"`, `"payload"`.

Annotations: `{"title":"Continuation checkpoint","readOnlyHint":false,"destructiveHint":true,"idempotentHint":true,"openWorldHint":false}`.

Reads, atomically writes, or clears one bounded non-secret JSON continuation
record scoped to the canonical selected project plus either `logical_task` or
`branch`. If neither scope argument is supplied, the selected repository's
current branch is used. Storage lives under DevMCP's private configuration root,
never inside the selected project; attempts to configure checkpoint storage
inside the project fail. Payload keys are restricted to task/slice, branch,
HEAD, PR/workflow identifiers, dirty-state summary, completed acceptance items,
next action, blocker type, and timestamp. Unknown or oversized payload data is
rejected, as are values matching common credential/private-key forms.

### antigravity_delegate

Inputs: `"prompt"`, `"mode"`, `"timeout_seconds"`, `"retry_transient"`, `"approval_id"`.

Annotations: `{"title":"Delegate to Antigravity","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Runs one bounded host-side Antigravity CLI task in a temporary detached Git
worktree at the selected project's current HEAD. It is controlled by
`agent.delegate`, which is automatically allowed by the built-in Autonomous
profile and may also be explicitly configured by Custom. The live checkout must
have no tracked or staged changes; untracked files are not copied into the
delegate worktree. Sensitive tracked paths block delegation entirely.
The binary is discovered from `DEVMCP_ANTIGRAVITY_BIN`, the service `PATH`,
`~/.local/bin/agy`, or `/usr/local/bin/agy`, in that order.

The delegate receives an explicit untrusted-data/prompt-injection boundary and a
filtered environment. DevMCP snapshots the MCP session's selected project once,
uses that same path for Git preflight/worktree creation/patch application, and
launches AGY with `--new-project --sandbox` from the isolated worktree derived
from that selected project. A pre-exec guard verifies the child's actual cwd is
the isolated worktree and fails before AGY starts if it differs. Delegation is
rejected if the installed CLI does not advertise both `--new-project` and
`--sandbox`; DevMCP does not fall back to AGY's default/persisted project or to
an unsandboxed launch. Its process cannot use the selected repository's real Git
remote through inherited Git configuration. After completion,
DevMCP rejects Git-history changes, file deletes/moves, sensitive-path changes,
and any modification in `read_only` or `verify` mode. `workspace_edit` applies
only a validated `M`/`A` binary patch to the real selected checkout. DevMCP
attempts to discard rejected, failed, or timed-out delegate work with the
temporary worktree; any worktree-removal failure is reported explicitly.
The delegate and its descendants run in a bounded process group. On POSIX,
DevMCP terminates any remaining members of that group before an attempt is
accepted, retried, or returned as an error, including ordinary non-zero exits;
timeout or MCP request cancellation also terminates the group before cleanup.
Windows retains the existing direct-process cleanup behavior. `timeout_seconds`
accepts 1-3600 seconds. With `retry_transient=true`, DevMCP retries at most once
for a timeout or an upstream 502/503-style transient failure; otherwise those
failures are returned as retryable structured errors. A zero OS exit code is
only process success: when JSON output contains `status: "ERROR"`, DevMCP returns
a task failure (`ok: false`) instead. Successful results expose `process_ok`,
`task_ok`, `agent_status`, `selected_workspace`, and `delegated_workspace` so the
workspace propagation and process/task outcome are observable.

### view_image

Inputs: `"path"`, `"max_bytes"`, `"max_width"`, `"max_height"`, `"auto_resize"`.

Annotations: `{"title":"View image","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

The base64 data appears exactly once, in one MCP image content block. Stable
`structuredContent` contains metadata only; it has no duplicate base64 or data
URL. Pillow is optional and used only for requested auto-resize.

## Forbidden product-layer tools

The runtime does not expose external-agent login/accounts, agent memory, cloud
tasks, web search/fetch, image generation, model routing, plugin installation,
or arbitrary subagent orchestration. `antigravity_delegate` is the sole bounded
delegation primitive and follows the isolation/validation contract above.

## Compatibility note for 0.2

0.1 clients that parsed the text block as JSON must switch to
`structuredContent`. The machine fields are retained where practical, while the
text block is now a concise human/model summary. Removed compatibility surfaces
are tool profiles, the `view_image.output` selector, duplicate image data URLs,
and JSON-RPC batches.
