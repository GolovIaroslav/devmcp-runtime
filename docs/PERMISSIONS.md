# Permissions

Policy is data, not MCP schema. Profiles are `safe`, `balanced`, `power`, and
`custom`; each capability resolves to `auto`, `ask`, or `deny`. The capability
matrix is the runtime's top-level authority: a selected profile controls patch,
exec, network, dependency, migration, Git, server-bind, and sensitive-
environment behavior. Legacy `safe`/`trusted`/`dangerous` modes are only
compatibility presets when no profile is selected.

```bash
devmcp policy profile balanced
devmcp policy export --file policy.json
devmcp policy import policy.json
```

Balanced is intended for ordinary development. It auto-allows read/search,
small patches, and registered safe tasks. Network, dependency changes,
migrations, public listeners, push, sensitive environment injection, and
destructive patches ask. `agent.delegate` is displayed as an unavailable
capability and is fixed to `deny`, because this release exposes no delegation
tool. This is an availability constraint, not a hidden host-boundary policy.
Actual host-boundary controls (bwrap, authentication, and loopback defaults)
apply independently of profile choice.

## ChatGPT app permissions are a separate layer

When DevMCP is connected through ChatGPT, there are two independent permission
layers:

1. ChatGPT app permissions decide whether ChatGPT asks before invoking an app
   action.
2. DevMCP local policy and approvals decide whether the runtime allows, asks for,
   or denies the operation.

The first layer cannot override the second, and DevMCP cannot programmatically
disable ChatGPT approval prompts. Current ChatGPT app modes are `Always ask`,
`Any changes`, `Important actions`, and `Never ask`. `Important actions` is the
usual cause of intermittent dialogs: reads and low-risk actions may run
automatically while higher-impact actions ask. If the workspace exposes the
choice, `Never ask` can be selected for a trusted local DevMCP app, but it
disables ordinary ChatGPT confirmation prompts and should only be used with an
MCP server you trust. Especially risky actions may still be blocked by ChatGPT.
Business, Enterprise, and Edu workspace administrators may control persistent
app permissions.

For the current ChatGPT requirements and Pro limitation, see the [OpenAI full
MCP guidance](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt),
[permission mode documentation](https://help.openai.com/en/articles/11487775-connectors-in-chatgpt),
and [workspace admin controls](https://help.openai.com/en/articles/11509118-admin-controls-security-and-compliance-for-plugins-and-apps).
