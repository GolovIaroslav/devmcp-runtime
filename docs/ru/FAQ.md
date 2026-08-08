# FAQ

**Это продукт OpenAI?** Нет, это независимый MCP runtime.

**Нужен ли ChatGPT?** Нет. Core работает с любым MCP-клиентом.

**Power — это полный доступ к host?** Нет. Он расширяет разрешения внутри
выбранного sandbox, но minimum security floor сохраняется.

**Где секреты?** Вне workspace, в `~/.config/devmcp-runtime/secrets/`, режим
файлов `0600`.
