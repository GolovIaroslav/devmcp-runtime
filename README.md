# Coding Tools MCP

Coding Tools MCP is a model-neutral coding-agent runtime MCP server. It exposes local coding primitives to any MCP client:

```text
inspect repo -> search/read files -> apply structured patches -> run tests/commands
-> interact with stdin sessions -> inspect git status/diff
```

## Demo Video

[![Watch the demo](https://img.youtube.com/vi/N9lQaXt1eqQ/maxresdefault.jpg)](https://youtu.be/N9lQaXt1eqQ?si=LyEwvzzQF6QjUxR0)

It is not a prompt wrapper. It does not expose external agent accounts, memory, cloud tasks, web search, image generation, model routing, plugin marketplace, or subagent orchestration as MCP tools.

## Documentation Map

- [Quickstart](docs/quickstart.md)
- [MCP client configuration](docs/mcp-client-config.md)
- [Embedding in your app or agent](docs/embedding.md)
- [Remote MCP](docs/remote-mcp.md)
- [Cloudflare sandbox control worker](cloudflare/sandbox-control/README.md)
- [npm launcher](npm/coding-tools-mcp/README.md)
- [Tools and schemas](docs/tools-and-schemas.md)
- [Permission modes](docs/permission-modes.md)
- [Exec command recipes](docs/exec-command-recipes.md)
- [Docker sandbox](docs/docker.md)
- [Security policy](SECURITY.md)
- [Security boundary](docs/security-boundary.md)
- [CI and test commands](docs/ci-and-tests.md)
- [Dogfood](docs/dogfood.md)
- [SWE-bench evaluation](docs/swe-bench.md)
- [Known limitations](docs/limitations.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Exec troubleshooting](docs/troubleshooting-exec.md)
- [Competitive analysis](docs/competitive-analysis.md)
- Normative runtime contract: [docs/runtime-contract-v0.2.md](docs/runtime-contract-v0.2.md)

## Quickstart

Install the published command from PyPI:

```bash
curl -fsSL https://raw.githubusercontent.com/xyTom/coding-tools-mcp/main/scripts/install.sh | bash
```

Install and start local Streamable HTTP against a workspace:

```bash
curl -fsSL https://raw.githubusercontent.com/xyTom/coding-tools-mcp/main/scripts/install.sh \
  | bash -s -- --start --workspace /path/to/repo
```

Install and expose an authenticated bearer-token tunnel:

```bash
curl -fsSL https://raw.githubusercontent.com/xyTom/coding-tools-mcp/main/scripts/install.sh \
  | bash -s -- --tunnel cloudflared --auto-install-tunnel --workspace /path/to/repo
```

Or, from this checkout:

```bash
scripts/install.sh
```

Run the published package without a persistent install:

```bash
uvx coding-tools-mcp --workspace .
```

Use stdio for MCP clients:

```bash
uvx coding-tools-mcp --stdio --workspace /path/to/repo
```

If your toolchain is Node-based, launch the same server through npm:

```bash
npx coding-tools-mcp --stdio --workspace /path/to/repo
```

The npm package is a launcher only. The server itself stays the Python package
on PyPI, which the launcher starts through `uvx` or `pipx run`, so `uv` or
`pipx` must be on `PATH`. The launcher runs the latest PyPI release; pin a
server version with `CODING_TOOLS_MCP_VERSION`:

```bash
CODING_TOOLS_MCP_VERSION=0.2.0 npx coding-tools-mcp --stdio --workspace /path/to/repo
```

The launcher's own npm version is independent of the server version it starts.

If you are working from this checkout instead of a published package:

```bash
make start
```

Pass a different workspace, host, port, or extra server flags with Make variables:

```bash
make start MCP_WORKSPACE=/path/to/repo MCP_PORT=8000 MCP_ARGS="--permission-mode trusted"
```

If dependencies are missing, install the runtime in editable mode:

```bash
python -m pip install -e ".[dev]"
```

Run the desktop client MVP:

```bash
python -m pip install -e ".[desktop]"
coding-tools-mcp-desktop
```

The desktop client follows the system language on first launch and can switch between English and Simplified Chinese at runtime.

HTTP endpoint:

```text
http://127.0.0.1:8765/mcp
```

Install the optional image extra when you want `view_image` auto-resize support:

```bash
python -m pip install -e ".[image]"
```

Stdio:

```bash
coding-tools-mcp --stdio --workspace /path/to/repo
```

Set `CODING_TOOLS_MCP_TRACE=1` to emit redacted JSON tool-call trace events to stderr for local debugging. Logs stay off stdout so stdio JSON-RPC remains clean.

By default, `exec_command` passes a core shell environment only. For local toolchains that depend on inherited environment variables, such as MSVC developer prompts, start with:

```bash
CODING_TOOLS_MCP_SHELL_ENV_INHERIT=all coding-tools-mcp --workspace /path/to/repo
```

`inherit=all` still filters secret-looking and loader/startup variables unless dangerous mode is also enabled. For local development with dependency downloads, shell expansion, and inline interpreter snippets, use:

```bash
coding-tools-mcp --permission-mode trusted --workspace /path/to/repo
```

`--allow-network` remains available as a compatibility flag when you only want to open network-looking commands. If your MCP client does not support permission elicitation and you explicitly want to disable `exec_command` permission gates inside an isolated container or VM, start with:

```bash
coding-tools-mcp --permission-mode dangerous --workspace /path/to/repo
```

This disables `exec_command` permission gates such as network-looking commands, destructive command checks, shell expansion, inline scripts, and sensitive env checks. Workspace path boundaries for direct file tools still apply. `--dangerously-skip-all-permissions` remains as a compatibility alias.

## MCP Client Examples

Generic stdio client:

```toml
[mcp_servers.coding_tools]
command = "uvx"
args = ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
```

Claude Code:

```json
{
  "mcpServers": {
    "coding-tools": {
      "command": "uvx",
      "args": ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

Cursor:

```json
{
  "mcpServers": {
    "coding-tools": {
      "command": "uvx",
      "args": ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

Clients that prefer a Node entry point can swap `uvx` for `npx` in any of the
examples above, keeping the same arguments:

```json
{
  "mcpServers": {
    "coding-tools": {
      "command": "npx",
      "args": ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

Generic Streamable HTTP clients should use MCP protocol version `2025-11-25`
and point at `http://127.0.0.1:8765/mcp`. Version `2025-06-18` remains
supported for existing clients.

## Remote MCP

For remote MCP clients and local development over an HTTPS tunnel, keep the
server bound to loopback and require bearer or OAuth authentication. The fixed
tool set contains command execution and workspace mutation, so an anonymous
public tunnel is unsafe:

```bash
CODING_TOOLS_MCP_AUTH_MODE=bearer \
./scripts/tunnel.sh cloudflared /path/to/repo
```

Configure the remote MCP client with the HTTPS tunnel URL:

```text
URL: https://<tunnel-host>/mcp
```

The tunnel scripts support `cloudflared`, `ngrok`, and Microsoft Dev Tunnel. If the selected tunnel CLI is missing, the script asks before installing it:

```bash
scripts/tunnel.sh cloudflared /path/to/repo
scripts/tunnel.sh ngrok /path/to/repo
scripts/tunnel.sh devtunnel /path/to/repo
```

For clients that support custom headers, use bearer-token auth with
`Authorization: Bearer <token>`. OAuth-aware clients can use OAuth 2.1
Authorization Code + PKCE by setting `CODING_TOOLS_MCP_AUTH_MODE=oauth`. The
server publishes RFC 7591 dynamic client registration, binds exact redirect
URIs, and can infer its issuer from a one-shot tunnel request. Set
`CODING_TOOLS_MCP_SERVER_URL` only to pin a stable issuer. Clients that support
neither bearer headers nor OAuth need an external authenticated proxy.

See [docs/remote-mcp.md](docs/remote-mcp.md) for the exact modes and security notes.

## Fixed Tool Set

The server exposes one stable catalog with truthful annotations. It does not
offer tool profiles, dynamically hide process tools, or provide `edit_file`.
`apply_patch` is the only direct file-mutation primitive. Permission modes
change command policy, not which tools the model sees.

## Tools

P0 tools exposed by default:

- `server_info`
- `check_exec_environment`
- `get_default_cwd`
- `set_default_cwd`
- `read_file`
- `list_dir`
- `list_files`
- `search_text`
- `apply_patch`
- `exec_command`
- `write_stdin`
- `kill_session`
- `read_output`
- `git_status`
- `git_diff`
- `git_log`
- `git_show`
- `git_blame`
- `request_permissions`

Additional image tool exposed by default:

- `view_image`

For input/output schemas and result envelopes, see
[docs/tools-and-schemas.md](docs/tools-and-schemas.md) and
[docs/runtime-contract-v0.2.md](docs/runtime-contract-v0.2.md).

Root `AGENTS.md`/`CLAUDE.md` instructions are loaded into the MCP initialize
context automatically; nested instruction files are indexed without eagerly
injecting their contents. No `open_workspace` tool call is required.

Tool `content` is concise agent-facing text, while `structuredContent` is the
complete stable machine result. Commands wait up to 10 seconds by default. Only
commands still running return a `write_stdin` next action; only truncated output
returns a `read_output` next action. Image base64 is emitted once in one MCP
image block.

## Safety Boundary

The runtime binds one workspace root per server process. Paths are workspace-relative by default. Absolute paths, `..` traversal, and symlink escapes are rejected. Recursive listing/search excludes `.git`, `.reference`, `node_modules`, `target`, `dist`, build outputs, virtualenvs, and common caches by default.

`exec_command` runs under policy controls with workspace-bound cwd, configurable shell environment inheritance, timeout, output caps, sensitive-value and loader/startup environment rejection, destructive command checks, network-looking command checks, shell-expansion permission gates, indirect absolute-path checks, cancellation/kill cleanup, session deadline watchdogs, and bounded session buffers. On Linux hosts with Landlock support it also applies filesystem confinement; on Windows, macOS, or Linux hosts without Landlock, command results include a warning and external sandboxing is required before running untrusted commands. This is still not a complete OS/container sandbox; see [SECURITY.md](SECURITY.md).

`--permission-mode safe` is the default. `--permission-mode trusted` opens local-development gates while keeping secret filtering and destructive-command checks. `--permission-mode dangerous` disables `exec_command` permission gates for operators who accept that risk inside an isolated runner. Do not use dangerous mode for untrusted workspaces or untrusted MCP clients.

## Compliance

```bash
make compliance
```

Compliance and CI commands are documented in [docs/ci-and-tests.md](docs/ci-and-tests.md). The checked-in report files are generated artifacts; inspect their `suite` field before treating them as full compliance evidence.

## Dogfood And Benchmark

Dogfood and SWE-bench notes live in [docs/dogfood.md](docs/dogfood.md), [docs/swe-bench.md](docs/swe-bench.md), and [BENCHMARK.md](BENCHMARK.md). This repository does not claim a model-generated SWE-bench leaderboard result.

## Development Commands

```bash
make lint
make typecheck
make test
make compliance
make ci
```

See [docs/ci-and-tests.md](docs/ci-and-tests.md) for the full test matrix.

## License

This project is licensed under the [Apache License 2.0](LICENSE).

If you use code, documentation, substantial implementation details, or
derivative work from this project, preserve the copyright notice, license
notice, and [NOTICE](NOTICE) file, and clearly attribute the original project.

Project: Coding Tools MCP  
Author: Coding Tools MCP Contributors  
Source: https://github.com/xyTom/coding-tools-mcp

Citation metadata is available in [CITATION.cff](CITATION.cff).
