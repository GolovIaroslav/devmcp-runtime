# Architecture

```mermaid
flowchart LR
  Client[MCP client] --> Runtime[DevMCP Runtime]
  Runtime --> Workspace[workspace tools]
  Runtime --> Patch[patch engine]
  Runtime --> Tasks[task registry]
  Runtime --> Sandbox[sandbox backend]
  Runtime --> Policy[policy data]
  Runtime --> UI[loopback UI]
  Tunnel[optional Secure MCP Tunnel] -.-> Runtime
```

The authoritative workspace is selected by configuration. Runtime state,
approval DB, audit log, config, and secrets live outside it. The model can use
workspace tools but cannot modify runtime policy or silently add roots.

The exposed MCP catalog has a schema version. Workflow breadth belongs in the
task registry and configuration, so clients do not need a new tool schema for
every supported ecosystem.
