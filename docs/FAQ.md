# FAQ

**Is this an OpenAI product?** No. It is an independent MCP runtime.

**Does it require ChatGPT?** No. Any MCP client can use the core runtime.

**Is Power unrestricted host execution?** No. It only broadens the active
capability matrix inside the selected sandbox; bwrap, path validation,
authentication, and loopback defaults still apply.

**What is Autonomous?** It is an explicit unattended-development profile. It
auto-authorizes every implemented DevMCP capability and adds first-class
host-side service diagnostics/restart, while keeping privilege escalation and
the runtime's hard workspace/sandbox boundaries denied.

**Where are secrets?** Outside the workspace under
`~/.config/devmcp-runtime/secrets/`, with `0600` permissions.

**Why is the tunnel optional?** Local MCP clients do not need it. It is only a
documented path for reaching a private local MCP server from supported clients.
