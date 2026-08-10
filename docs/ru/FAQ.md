# FAQ

**Это продукт OpenAI?** Нет, это независимый MCP runtime.

**Нужен ли ChatGPT?** Нет. Core работает с любым MCP-клиентом.

**Power — это полный доступ к host?** Нет. Он расширяет активную capability
matrix внутри выбранного sandbox; bwrap, проверка путей, authentication и
loopback defaults всё равно применяются.

**Что такое Autonomous?** Это явный профиль автономной разработки: все
реализованные capability выполняются без локальных approval, а status/doctor/
restart DevMCP доступны как first-class host-side tools. Privilege escalation и
hard workspace/sandbox boundary при этом не отключаются.

**Где секреты?** Вне workspace, в `~/.config/devmcp-runtime/secrets/`, режим
файлов `0600`.
