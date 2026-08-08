# FAQ

**Это продукт OpenAI?** Нет, это независимый MCP runtime.

**Нужен ли ChatGPT?** Нет. Core работает с любым MCP-клиентом.

**Power — это полный доступ к host?** Нет. Он расширяет активную capability
matrix внутри выбранного sandbox; bwrap, проверка путей, authentication и
loopback defaults всё равно применяются.

**Где секреты?** Вне workspace, в `~/.config/devmcp-runtime/secrets/`, режим
файлов `0600`.
