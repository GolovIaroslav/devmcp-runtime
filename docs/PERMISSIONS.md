# Permissions

Policy is data, not MCP schema. Profiles are `safe`, `balanced`, `power`,
`autonomous`, and `custom`; each capability resolves to `auto`, `ask`, or `deny`. The capability
matrix is the runtime's top-level authority: a selected profile controls patch,
exec, network, dependency, migration, Git, server-bind, and sensitive-
environment behavior. Legacy `safe`/`trusted`/`dangerous` modes are only
compatibility presets when no profile is selected.

```bash
devmcp policy profile balanced
devmcp policy profile autonomous
devmcp policy export --file policy.json
devmcp policy import policy.json
```

Balanced is intended for ordinary development. It auto-allows read/search,
small patches, and registered safe tasks. Network, dependency changes,
migrations, public listeners, push, sensitive environment injection, and
destructive patches ask. `agent.delegate` is implemented by the bounded
`antigravity_delegate` tool but remains `deny` in Safe, Balanced, and Power.
Autonomous auto-authorizes it, while Custom may explicitly choose its decision.
Actual host-boundary controls (bwrap, authentication, and loopback defaults)
apply independently of profile choice.

Autonomous is the opt-in operator profile for unattended development. Every
implemented capability, including arbitrary sandboxed exec, network, dependency
changes, destructive workspace operations, Git push, sensitive environment
injection, public binds, bounded Antigravity delegation, and DevMCP user-service
management, resolves to `auto`. It removes DevMCP's local approval queue from
those operations; it does not disable the hard runtime boundary. Privilege-escalation executables,
setuid/setgid execution, paths outside the selected project, sandbox escapes,
and Docker/Podman socket exposure remain denied. `service_status` and
`service_doctor` inspect the host-side DevMCP service directly, while
`service_restart` schedules a delayed trusted CLI restart so the MCP response
can complete before the current service is replaced. The restart waits for MCP
health before restarting the tunnel, avoiding listener/tunnel startup races.
`service_update` uses the same `service.manage` decision but accepts only a
clean local `devmcp-runtime` checkout on `main` with `HEAD == origin/main`; it
revalidates the pinned SHA before a user-level reinstall and safe restart.

Git synchronization has its own `git.sync` capability for fetch/prune and
fast-forward-only pull. Safe/Balanced ask, Power and Autonomous auto-authorize
it. Remote branch deletion remains governed by `git.push`.

Persistent profile changes use a separate `policy.manage` capability. Safe,
Balanced, and Power always ask; Autonomous auto-authorizes. The
`activate_policy_profile` tool persists the selected profile and schedules a
safe restart, so initial Autonomous activation never requires arbitrary shell
or direct config-file editing.

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
