# Интеграция с ChatGPT Developer Mode

Сначала установите DevMCP Runtime, выполните `devmcp setup`, затем запустите
локальный MCP и отдельно настроенный Secure MCP Tunnel. В ChatGPT Developer
Mode создайте custom MCP app и оставьте её draft/development.

Выберите Tunnel connection mode, настройте Bearer authentication и нажмите
**Scan Tools**. После изменения MCP-каталога обновите actions приложения.
Проверяйте сначала чтение и Balanced coding loop, затем очередь approvals.

Текущая инструкция OpenAI: <https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt>.
Официальный tunnel-client — отдельный проект: <https://github.com/openai/tunnel-client>.
DevMCP Runtime не является продуктом OpenAI и не использует OpenAI branding.
