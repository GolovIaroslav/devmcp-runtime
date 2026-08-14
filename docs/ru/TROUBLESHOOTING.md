# Диагностика

- `OpenAI Secure MCP Tunnel key is not configured` касается только tunnel;
  локальный MCP продолжает работать. Ключ можно ввести через `devmcp setup`.
- `SANDBOX_UNAVAILABLE` из старого PLAN/sandbox compatibility path не является требованием BUILD; текущий BUILD запускает процессы напрямую на host.
- BUILD не возвращает per-command `approval_required`; неожиданные permission errors нужно диагностировать как OS/runtime/tool-contract failures.
- После изменения tool schema обновите actions draft-приложения и повторите
  **Scan Tools**.
