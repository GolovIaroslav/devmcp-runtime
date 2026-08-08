# DevMCP Runtime

Локальный sandboxed coding runtime для MCP-клиентов: настраиваемые права,
точечные патчи, тесты и сборка, а также необязательная интеграция с ChatGPT
Developer Mode через Secure MCP Tunnel.

> Это независимый open-source проект. Он не связан с OpenAI, не одобрен OpenAI
> и не поддерживается OpenAI.

## Что это такое

DevMCP Runtime даёт MCP-клиенту локальное рабочее пространство с явной
политикой, движком патчей, реестром задач, sandboxed-выполнением процессов,
очередью подтверждений и локальной админ-панелью.

Поток выглядит так: MCP-клиент → DevMCP Runtime → patch engine, sandbox,
тесты/сборки, policy engine и loopback UI. Tunnel — отдельная необязательная
интеграция.

## Быстрый старт (Linux)

Нужны Python 3.11+, Git и bubblewrap (`bwrap`). Tunnel-клиент необязателен для
локальной работы.

```bash
uv tool install devmcp-runtime
devmcp setup --workspace /абсолютный/путь/к/проекту --no-tunnel
devmcp doctor
devmcp status
devmcp ui
```

Панель откроется на `http://127.0.0.1:47158`, локальный MCP-сервер использует
`127.0.0.1:47157`. Подробности интеграции с ChatGPT описаны в
[docs/ru/CHATGPT.md](docs/ru/CHATGPT.md).

## Возможности и права

Есть четыре data-driven профиля: `safe`, `balanced` (по умолчанию для нового
CLI), `power` и `custom`. Маленькие зарегистрированные coding-задачи в
Balanced выполняются автоматически; сеть, зависимости, миграции, push и
опасные патчи создают точечный запрос подтверждения. Delete и Move настраиваются
политикой и никогда не обходят проверки путей, symlink и аудит.

Даже Power не разрешает path traversal, symlink escape, доступ к чужой части
host filesystem, `~/.ssh`/`~/.aws`, privilege escalation, daemon sockets или
раскрытие секретов. Linux+bwrap — поддерживаемая security-платформа; macOS и
Windows пока экспериментальны и не обещают эквивалентную изоляцию.

## Проверки и вклад

```bash
make lint
make typecheck
make test
make dogfood-smoke
```

Сначала прочитайте [CONTRIBUTING.md](CONTRIBUTING.md). Уязвимости отправляйте
по инструкции в [SECURITY.md](SECURITY.md), вопросы — через
[SUPPORT.md](SUPPORT.md).

## Лицензия

Apache-2.0. Проект основан на `xyTom/coding-tools-mcp`; attribution сохранён в
[NOTICE](NOTICE) и [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
