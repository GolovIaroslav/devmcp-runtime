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
