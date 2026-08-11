# ChatGPT connection

DevMCP Runtime can be connected to ChatGPT as a custom MCP app when the account
and workspace support Developer Mode. The current full MCP write/modify
workflow requires ChatGPT Business, Enterprise, or Edu, ChatGPT on the web,
Developer Mode, and Secure MCP Tunnel for a local/private MCP server. ChatGPT
Pro currently supports custom MCP read/fetch access only, not the full
coding-agent write workflow.

Keep the MCP endpoint private, use the generated token file, and expose it only
through the supported secure tunnel workflow. DevMCP is independent software,
not an OpenAI product.

## Setup

1. Run `devmcp setup` and complete the local health check.
2. Run `devmcp tunnel status` and follow the tunnel-client instructions for the
   authenticated runtime connection.
3. In ChatGPT, create or edit a custom app and add the MCP endpoint and
   authentication configured by the tunnel flow.
4. Refresh the app's tools after changing the server card or schemas. A local
   policy change or service restart alone does not require Refresh; the MCP
   bearer token is preserved unless you explicitly rotate it.
5. Confirm that a harmless read-only tool works before enabling write tools.

ChatGPT's current UI and account capabilities determine which connection
options are available. See the official guidance:

- [Developer mode and full MCP connectors](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt)
- [Connector permission modes](https://help.openai.com/en/articles/11487775-connectors-in-chatgpt)
- [Workspace admin controls for apps](https://help.openai.com/en/articles/11509118-admin-controls-security-and-compliance-for-plugins-and-apps)
- [OpenAI Secure MCP Tunnel client](https://github.com/openai/tunnel-client)

## Two independent permission layers

There are two separate decisions in a ChatGPT → Secure MCP Tunnel → DevMCP
request:

1. ChatGPT app permissions decide whether ChatGPT asks before invoking an app
   action.
2. DevMCP local policy and approvals decide whether the local runtime allows,
   asks for, or denies the operation.

Changing one layer does not change the other. In particular, DevMCP cannot
programmatically disable ChatGPT's approval prompts, and selecting a less
restrictive ChatGPT mode does not weaken DevMCP's local policy or security
floor.

ChatGPT currently exposes these app permission modes:

- `Always ask`: ask before every action, including reads.
- `Any changes`: reads can run automatically; ask before changes.
- `Important actions`: ask before important or higher-impact actions; this is
  normally why a user sees intermittent approval dialogs while reads and
  lower-risk actions run automatically.
- `Never ask`: allow app actions without ordinary confirmation prompts.

If the workspace UI makes an app-specific setting available, a user who
intentionally trusts their local DevMCP instance can select `Never ask` for the
DevMCP app. `Never ask` should only be used with an MCP server the user trusts:
it disables ordinary ChatGPT app confirmation prompts. Especially risky actions
may still be blocked by ChatGPT even with `Never ask`. In managed Business,
Enterprise, and Edu workspaces, persistent app permissions can be controlled by
workspace administrators.

These ChatGPT app settings do not grant local filesystem access. DevMCP still
evaluates its profile, capability rules, approvals, path checks, sandbox, and
secret protections for every operation. See [Permissions](PERMISSIONS.md) and
[permission modes](permission-modes.md) for the local layer.

Never paste the MCP token or control-plane API key into a prompt, issue,
screenshot, log, or public repository.

For autonomous coding clients that need to survive CI waits, provider retries,
or client/session interruption, follow the
[autonomous continuation protocol](AGENT_AUTONOMY.md). It keeps provider polling
with the connector that owns the credentials and uses DevMCP only for bounded
waiting and private, project-scoped continuation checkpoints.
