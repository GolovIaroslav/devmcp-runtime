# Архитектура

MCP-клиент обращается к DevMCP Runtime, который связывает workspace tools,
patch engine, task registry, sandbox, policy data и loopback UI. Secure MCP
Tunnel — необязательная внешняя интеграция.

Авторитетный workspace, конфиг, approval DB, audit log и секреты разделены.
Модель не может сама добавить корень workspace или изменить security policy.
Каталог MCP имеет версию схемы; новые workflow добавляются в registry и data.
