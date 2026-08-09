# Интеграция с ChatGPT Developer Mode

Сначала установите DevMCP Runtime, выполните `devmcp setup`, затем запустите
локальный MCP и отдельно настроенный Secure MCP Tunnel. В ChatGPT Developer
Mode создайте custom MCP app и оставьте её draft/development.

Выберите Tunnel connection mode, настройте Bearer authentication и нажмите
**Scan Tools**. После изменения MCP-каталога обновите actions приложения.
Проверяйте сначала чтение и Balanced coding loop, затем очередь approvals.

Здесь есть два независимых слоя прав. Разрешения приложения ChatGPT определяют,
спросит ли ChatGPT подтверждение перед action. Локальная policy и approvals
DevMCP определяют, разрешит ли runtime операцию, запросит ли локальное approval
или отклонит её. DevMCP не может программно отключить подтверждения ChatGPT.

Текущие требования полного workflow с записью: ChatGPT Business, Enterprise или
Edu, web-версия ChatGPT, Developer Mode и Secure MCP Tunnel для локального/private
MCP. ChatGPT Pro сейчас поддерживает custom MCP только для чтения/fetch, не полный
coding-agent workflow с записью.

В ChatGPT доступны режимы `Always ask`, `Any changes`, `Important actions` и
`Never ask`. `Important actions` обычно даёт периодические диалоги: чтение и
малорисковые операции проходят автоматически, важные действия спрашиваются.
Если интерфейс workspace это позволяет и сервер доверенный, для приложения можно
выбрать `Never ask`. Этот режим отключает обычные подтверждения ChatGPT, поэтому
не используйте его с недоверенным MCP; особенно рискованные действия всё равно
могут быть заблокированы. В управляемых workspace постоянные разрешения может
задавать администратор.

Текущая инструкция OpenAI: <https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt>.
Официальный tunnel-client — отдельный проект: <https://github.com/openai/tunnel-client>.
DevMCP Runtime не является продуктом OpenAI и не использует OpenAI branding.
