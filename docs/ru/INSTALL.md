# Установка

Для beta поддерживается Linux. Установите Python 3.11+, Git и bubblewrap.
Tunnel-клиент нужен только для интеграции с удалённым MCP-клиентом.

```bash
uv tool install devmcp-runtime
devmcp setup --workspace /абсолютный/путь/к/репозиторию
devmcp doctor
```

`devmcp service install` создаёт только user units systemd: без sudo, без
system-wide службы и без удаления данных.
