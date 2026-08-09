# Configuration

Non-secret configuration lives at `~/.config/devmcp-runtime/config.toml`.
Secrets live separately at `secrets/mcp-token` and
`secrets/control-plane-api-key`; the directory is `0700` and secret files are
`0600`.

```bash
devmcp config show
devmcp config validate
devmcp config get workspace
devmcp config set ui_port 47158
devmcp auth status
```

The loader imports known legacy paths under `~/.config/chatgpt-dev-runtime/`
and `~/.config/tunnel-client/` without deleting them. Config writes are atomic
and include a schema version. Existing MCP tokens are preserved on repeated
`devmcp setup` runs; rotate one explicitly with `devmcp auth rotate-mcp-token`.
The legacy tunnel profile name and tunnel ID are imported when present.
