# Permission Modes

`exec_command` has three permission modes.

## safe

Default mode. Commands run with the coding-agent policy:

- workspace read/write
- system toolchain and DNS resolver paths read-only
- `HOME`, `TMPDIR`, and `cache_dir` under private directories inside the sandbox
- registered non-network tasks (for example pytest, unittest, Vitest, Jest,
  lint, typecheck, and build/check workflows) auto-allowed
- network-looking commands, unknown commands, shell expansion, and inline
  interpreter snippets return an out-of-band `approval_required` record
- secret-looking and loader/startup env filtered
- Landlock enabled when available

The same policy auto-allows read-only workspace and Git inspection, patch
preview, and small safe Add/Update patches. Delete/Move patches require the
active data-policy approval (Safe and Balanced ask; Power can allow them).
Paths outside the authoritative workspace, secrets, sandbox escape, privileged
commands, and Docker/Podman socket exposure are always denied. Large updates (more than 200
removed existing lines or more than 30% of an existing file) require local
ApprovalEngine approval.

Approval is not MCP elicitation. The server returns an approval ID and the
operator runs `devmcp approve <id>` locally; the model retries the exact
operation with that ID.

Start explicitly:

```bash
coding-tools-mcp --permission-mode safe --workspace /path/to/repo
```

## trusted

Local development mode. It allows dependency downloads, shell expansion, and inline interpreter snippets while keeping secret filtering and destructive-command checks.

`HOME`, `TMPDIR`, and `cache_dir` use private directories inside the sandbox.
The child also receives a fresh `--tmpfs /tmp`; host `/tmp` is not bind-mounted
and is not a writable escape route.

```bash
coding-tools-mcp --permission-mode trusted --workspace /path/to/repo
```

## dangerous

Dangerous mode disables `exec_command` permission gates and Landlock. Use it only inside an isolated container or VM.

```bash
coding-tools-mcp --permission-mode dangerous --workspace /path/to/repo
```

Compatibility aliases:

- `--allow-network`: opens only the network-looking command gate.
- `--dangerously-skip-all-permissions`: alias for `--permission-mode dangerous`.

## Client-Side Annotation Gates

Permission modes govern this server's own gates. They cannot affect a client that
gates on MCP annotations — one that refuses to call, or prompts on every call to, a
tool advertised as mutating. That friction lives entirely in the client, so
`--permission-mode dangerous` does nothing about it.

`--dangerously-fake-readonly-annotations` is retained only as an explicitly
dangerous test/debug compatibility switch. It makes `tools/list` report every
tool with `readOnlyHint: true`, `destructiveHint: false`, and `openWorldHint: false`:

```bash
coding-tools-mcp --permission-mode dangerous \
  --dangerously-fake-readonly-annotations --workspace /path/to/repo
```

Do not use this switch to avoid ChatGPT confirmation prompts. The annotations are
false: `apply_patch` still rewrites or deletes files, `run_task` still executes
registered tasks, and `exec_command` still runs commands; only the advertised
hints change. Because the claim is false, it is fenced in:

- It requires `--permission-mode dangerous`, so it can only be set alongside an
  explicit assertion that the workspace is disposable.
- Over HTTP it requires bearer auth or OAuth. A tunnel forwards to a loopback bind,
  so the bind address cannot distinguish a private sandbox from a publicly reachable
  one; authentication can. Use stdio for an unauthenticated local sandbox.
- `server_info.annotation_override` and the server card's
  `tools.annotationOverride` report `fake_readonly`, and both keep listing the real
  per-tool annotations. `check_exec_environment` adds a warning. The lie is confined
  to `tools/list`, so ground truth is always one call away.

`CODING_TOOLS_MCP_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS=1` is equivalent. This is
not a tool profile: the catalog is unchanged and every tool remains callable.
It is never the recommended solution for client confirmation behavior; use the
ChatGPT app permission controls and keep truthful MCP annotations instead.

## Runtime Directory

Safe and trusted modes keep command runtime state outside the Git worktree:

```text
/tmp/coding-tools-mcp/<workspace-hash>/<instance-id>/
  home/       # host-side server state; bwrap children get private equivalents
  tmp/        # host-side server state; bwrap children use sandbox/.devmcp-tmp
  cache/      # host-side server state; bwrap children use sandbox/.devmcp-cache
```

On Windows, the parent is the platform temp directory instead of `/tmp`. The server creates these directories lazily when `exec_command` first needs an environment. `server_info` and `check_exec_environment` report `runtime_dir`, `home`, `tmpdir`, and `cache_dir`.

The server does not create workspace-local `.coding-tools/` directories by default. Runtime directories are per server instance; after stopping the server, operators may remove an instance directory or the whole external runtime tree. Bwrap removes the sandbox-owned private directories with the sandbox. Normal OS temp cleanup may also remove stale host-side directories.

Set `CODING_TOOLS_MCP_RUNTIME_ROOT` to choose an explicit external runtime parent. The server reports `RUNTIME_DIR_UNWRITABLE` instead of falling back into the workspace for runtime state.
