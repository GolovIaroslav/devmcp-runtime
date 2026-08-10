# Coding Tools MCP Runtime Contract v0.2

Status: implemented contract for `coding-tools-mcp` 0.2.x.

Protocol target: MCP `2025-11-25`, with explicit compatibility for `2025-06-18`.

This contract describes one stable, model-neutral coding tool set. There are no
tool profiles and the server does not add or remove process tools dynamically.
`apply_patch` is the only direct file-mutation primitive; `edit_file` is not
provided. Permission modes alter command policy, not the advertised catalog.

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
symlinks. Each Streamable HTTP MCP session owns an independent active project;
`select_project` changes that session only and never mutates the configured
roots. Selection uses the existing checkout directly and does not create a
worktree. The initialize instructions tell clients to discover/select a named
project and then read its returned root authority files before modifying code.

## Workspace and patch guarantees

- One MCP-session runtime owns one canonical active repository root at a time.
- Direct path inputs are workspace-relative. Absolute paths, `..` traversal,
  NUL bytes, and symlink escapes are rejected.
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
["ABSOLUTE_PATH_DENIED", "ACCESS_DENIED", "BINARY_FILE", "GIT_ERROR", "INTERNAL_ERROR", "INVALID_ARGUMENT", "INVALID_STATE", "IS_DIRECTORY", "NOT_A_DIRECTORY", "NOT_FOUND", "NOT_IMPLEMENTED", "OUTPUT_TOO_LARGE", "PATCH_BASELINE_LIMIT", "PATCH_CONFLICT", "PATCH_CONTEXT_AMBIGUOUS", "PATCH_CONTEXT_NOT_FOUND", "PATCH_FAILED", "PATCH_HUNKS_OVERLAP", "PATCH_ROLLBACK_FAILED", "PATH_OUTSIDE_WORKSPACE", "PERMISSION_REQUIRED", "RUNTIME_DIR_UNWRITABLE", "SANDBOX_FAILED", "SANDBOX_UNAVAILABLE", "SESSION_CLOSED", "SESSION_LIMIT_REACHED", "SESSION_NOT_FOUND", "SYMLINK_ESCAPE", "TTY_UNSUPPORTED", "UNSUPPORTED_ENCODING"]
```

Error categories are `validation`, `security`, `permission`, `runtime`,
`not_found`, `conflict`, and `internal`.

Malformed JSON-RPC uses standard protocol errors: parse `-32700`, invalid
request `-32600`, unknown method `-32601`, invalid params/tool `-32602`, and
unexpected server failure `-32603`.

## Process lifecycle

`exec_command`, `write_stdin`, `read_output`, and `kill_session` are always in
the catalog. `exec_command` and `write_stdin` default to a 10-second yield. A
short command normally finishes in one call. A running command returns:

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

Execution snapshots have explicit runtime ownership. A sandbox is created only
under the runtime's private `sandboxes/` directory and carries an ownership
marker outside the writable snapshot. Active command sessions hold leases on
the snapshot; normal exit, non-zero exit, timeout, kill/cancellation, startup
failure, and runtime shutdown release those leases. The last lease removes the
snapshot synchronously before terminal completion is reported. Cleanup verifies
the canonical owned path and marker before removal, so it cannot delete the
selected repository or an unrelated caller-owned path. Completed commands do
not retain repository-sized sandbox copies for later commands.

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

The default catalog has 41 tools, including `view_image`. Setting
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

Changes only this MCP runtime's navigation base; it does not modify files.

### list_projects

Inputs: none.

Annotations: `{"title":"List projects","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Discovers Git repositories under operator-approved project roots. Discovery is
bounded, canonicalized, does not follow symlinks, and does not change the active
project.

### select_project

Inputs: `"project"`.

Annotations: `{"title":"Select project","readOnlyHint":false,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Selects exactly one discovered repository for the current MCP session. Running
command sessions prevent switching. Existing execution sandboxes are discarded,
the new repository becomes the file/patch/exec/Git root, and project context is
reloaded. No configured root is added or persisted.

### current_project

Inputs: none.

Annotations: `{"title":"Current project","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns the selected repository, its operator root, relative path, and known
root authority files.

### project_checks

Inputs: none.

Annotations: `{"title":"Project checks","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns bounded project-native verification argv. Repository Makefile targets
are preferred. A uv Python fallback uses `uv run --offline --frozen --no-sync`
and never silently chooses host bare `pytest`.

### run_project_check

Inputs: `"check_id"`, `"timeout_ms"`, `"yield_time_ms"`, `"max_output_bytes"`, `"approval_id"`.

Annotations: `{"title":"Run project check","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Runs only an argv returned by `project_checks`, inside the selected repository's
normal execution sandbox. It does not automatically install dependencies or
grant network access.

### read_file

Inputs: `"path"`, `"start_line"`, `"end_line"`, `"max_lines"`, `"max_bytes"`, `"encoding"`.

Annotations: `{"title":"Read file","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Reads UTF-8 ranges as a stream, reports full file line/byte metadata, rejects
binary content, and returns continuation metadata when bounded.

### read_files

Inputs: `"paths"`.

Annotations: `{"title":"Read files","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Reads a bounded list of UTF-8 workspace-relative files without following
symlinks outside the workspace.

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

### apply_patch

Inputs: `"patch"`, `"dry_run"`, `"approval_id"`.

Annotations: `{"title":"Apply patch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Supports `*** Add File` and `*** Update File` inside a
`*** Begin Patch` / `*** End Patch` envelope. Delete and move operations are
parsed by the release policy layer: Safe and Balanced require local approval,
while Power may allow them. Preview returns a unified diff, line counts, removal
percentage, and risk classification. Small updates execute immediately; an
update above either configured destructive threshold requires a single-use
local out-of-band approval.

### exec_command

Inputs: `"cmd"`, `"cwd"`, `"workdir"`, `"timeout_ms"`, `"yield_time_ms"`, `"env"`, `"max_bytes"`, `"max_output_bytes"`, `"preview_bytes"`, `"tty"`, `"stdin"`, `"verbosity"`, `"network_required"`, `"task_id"`, `"approval_id"`.

Annotations: `{"title":"Exec command","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Statuses are `exited`, `running`, `timeout`, `terminated`, or `failed`.
Launch/policy failures use the error envelope with `status: "failed"`; signal
exits use `terminated`. Ordinary non-zero exit codes still use `exited`.

`approval_id` consumes one immutable approval for the exact command, normalized
cwd, raw environment delta, task/session identity, network capability, and
policy version. Approved capabilities are applied to the corresponding policy
gates only.

### run_task

Inputs: `"task_id"`, `"args"`, `"path"`, `"cwd"`, `"env"`, `"timeout_ms"`, `"yield_time_ms"`, `"max_output_bytes"`, `"approval_id"`.

Annotations: `{"title":"Run task","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Tasks use validated registry metadata and an argv-only subprocess path. Known
non-network tasks such as pytest, unittest, Vitest, Jest, lint, typecheck, and
build/check workflows execute automatically in the sandbox. Network and other
approval-class capabilities are granted per operation and are never inherited
from an unrelated command.

### write_stdin

Inputs: `"session_id"`, `"chars"`, `"yield_time_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`.

Annotations: `{"title":"Write stdin","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Poll or interact with a command session. Pass empty `chars` to wait for output.

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
Arbitrary URL remotes and force push are rejected. `git.push` remains
approval-gated by policy, and failed push output is withheld to avoid credential
disclosure.

### view_image

Inputs: `"path"`, `"max_bytes"`, `"max_width"`, `"max_height"`, `"auto_resize"`.

Annotations: `{"title":"View image","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

The base64 data appears exactly once, in one MCP image content block. Stable
`structuredContent` contains metadata only; it has no duplicate base64 or data
URL. Pillow is optional and used only for requested auto-resize.

## Forbidden product-layer tools

The runtime does not expose external-agent login/accounts, agent memory, cloud
tasks, web search/fetch, image generation, model routing, plugin installation,
subagent orchestration, or high-level prompt wrappers.

## Compatibility note for 0.2

0.1 clients that parsed the text block as JSON must switch to
`structuredContent`. The machine fields are retained where practical, while the
text block is now a concise human/model summary. Removed compatibility surfaces
are tool profiles, the `view_image.output` selector, duplicate image data URLs,
and JSON-RPC batches.
