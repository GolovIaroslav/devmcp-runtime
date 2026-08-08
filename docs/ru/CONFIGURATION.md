# Конфигурация

Обычная конфигурация находится в `~/.config/devmcp-runtime/config.toml`, а
секреты — отдельно в `secrets/mcp-token` и
`secrets/control-plane-api-key`. Каталог имеет режим `0700`, файлы — `0600`.

```bash
devmcp config show
devmcp config validate
devmcp config get workspace
devmcp auth status
```

Известные legacy-пути импортируются без удаления исходных файлов. Запись
конфигурации атомарна и содержит номер схемы. Повторный `devmcp setup`
сохраняет существующий MCP-токен; для явной ротации используйте `devmcp auth
rotate-mcp-token`. При наличии импортируются имя legacy tunnel-профиля и
tunnel ID.
