# Диагностика

- `OpenAI Secure MCP Tunnel key is not configured` касается только tunnel;
  локальный MCP продолжает работать. Ключ можно ввести через `devmcp setup`.
- `SANDBOX_UNAVAILABLE`: проверьте установку `bwrap`; unsafe fallback не должен
  включаться незаметно.
- `approval_required`: покажите `devmcp approvals`, подтвердите именно эту
  операцию и повторите один раз. Повтор consumed approval должен быть отвергнут.
- После изменения tool schema обновите actions draft-приложения и повторите
  **Scan Tools**.
