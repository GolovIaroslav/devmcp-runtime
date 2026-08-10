# Права

Политика хранится как данные и не меняет MCP-схемы. Профили: `safe`,
`balanced`, `power`, `autonomous`, `custom`; для каждой capability задаётся `auto`, `ask`
или `deny`.

В Balanced чтение, поиск, маленькие патчи и зарегистрированные безопасные
задачи выполняются автоматически. Сеть, зависимости, миграции, push,
чувствительное окружение и разрушительные патчи требуют подтверждения.
Минимальный security floor действует во всех профилях.

`autonomous` — явный режим для автономной разработки без локальной очереди
approval: все реализованные capability получают `auto`, включая arbitrary exec
в sandbox, сеть, зависимости, destructive workspace-операции, Git push,
sensitive env, public bind и управление user-service DevMCP. Hard boundary при
этом не отключается: privilege escalation (`sudo`, `su`, `doas`), setuid/setgid,
выход за выбранный project, sandbox escape и Docker/Podman socket по-прежнему
запрещены. Для host-side обслуживания доступны `service_status`,
`service_doctor` и отложенный `service_restart`; restart ждёт успешный MCP
health перед рестартом tunnel, чтобы не ловить race при запуске listener.

Для Git sync используется отдельная capability `git.sync`: fetch с prune и
только fast-forward pull. Safe/Balanced требуют approval, Power/Autonomous
разрешают автоматически. Удаление remote branch контролируется `git.push`.

Разрешения приложения ChatGPT и локальная policy DevMCP — независимые слои:
первый определяет диалоги подтверждения ChatGPT, второй — allow/ask/deny внутри
runtime. `Never ask` отключает обычные подтверждения только для приложения и
должен использоваться только с доверенным сервером; DevMCP не может отключить
эти диалоги программно, а особенно рискованные действия ChatGPT всё равно может
заблокировать. В Business/Enterprise/Edu постоянные app permissions может
контролировать администратор. См. [актуальную документацию OpenAI](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt).
