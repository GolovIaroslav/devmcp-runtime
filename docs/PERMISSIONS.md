# Permissions

Policy is data, not MCP schema. Profiles are `safe`, `balanced`, `power`, and
`custom`; each capability resolves to `auto`, `ask`, or `deny`.

```bash
devmcp policy profile balanced
devmcp policy export --file policy.json
devmcp policy import policy.json
```

Balanced is intended for ordinary development. It auto-allows read/search,
small patches, and registered safe tasks. Network, dependency changes,
migrations, public listeners, push, sensitive environment injection, and
destructive patches ask. The minimum security floor remains denied in every
profile.
