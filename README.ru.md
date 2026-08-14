# DevMCP Runtime

Локальный coding runtime для MCP-клиентов. BUILD используется по умолчанию и
работает с полномочиями текущего OS-user для файловой системы, окружения и сети;
PLAN остаётся read-only. Выбранный проект — coding context, а не security boundary
для BUILD. Интеграция с ChatGPT Developer Mode через Secure MCP Tunnel необязательна.

> Это независимый open-source проект. Он не связан с OpenAI, не одобрен OpenAI
> и не поддерживается OpenAI.

## Что это такое

DevMCP Runtime даёт MCP-клиенту локальный coding context, движок патчей, реестр
задач, прямое выполнение процессов в BUILD, read-only режим PLAN и локальную
админ-панель. High-level Git tools остаются scoped к выбранному проекту, хотя
BUILD filesystem operations следуют обычным OS permissions.

Поток выглядит так: MCP-клиент → DevMCP Runtime → patch engine, BUILD host
execution / PLAN read-only, тесты/сборки и loopback UI. Tunnel — отдельная
необязательная интеграция.

## Быстрый старт (Linux)

Нужны Python 3.11+ и Git. Tunnel-клиент необязателен для локальной работы.
Bubblewrap не требуется для обычного выполнения в BUILD.

До первой публикации в PyPI устанавливайте проект напрямую из текущего
репозитория:

```bash
uv tool install git+https://github.com/GolovIaroslav/devmcp-runtime.git
devmcp setup --workspace /абсолютный/путь/к/проекту --no-tunnel
devmcp doctor
devmcp status
devmcp ui
```

После намеренной публикации `devmcp-runtime` в PyPI с настроенным trusted
publishing release-команда будет такой:

```bash
uv tool install devmcp-runtime
```

Панель откроется на `http://127.0.0.1:47158`, локальный MCP-сервер использует
`127.0.0.1:47157`. Подробности интеграции с ChatGPT описаны в
[docs/ru/CHATGPT.md](docs/ru/CHATGPT.md).

### Разрешения ChatGPT и режимы выполнения DevMCP

Разрешения приложения ChatGPT и локальный execution mode DevMCP — независимые слои:

- разрешения приложения ChatGPT определяют, спросит ли ChatGPT подтверждение
  перед вызовом action;
- DevMCP один раз разрешает режим при старте: PLAN остаётся read-only, а BUILD
  работает с полномочиями текущего OS-user без per-command approval gates.

Для полного MCP workflow с записью/изменением кода текущие требования OpenAI:
ChatGPT Business, Enterprise или Edu, ChatGPT в web, Developer Mode и Secure
MCP Tunnel для локального/private MCP-сервера. ChatGPT Pro сейчас поддерживает
custom MCP только для чтения/fetch, а не полный coding-agent workflow с записью.
См. [текущую документацию OpenAI](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt).

Режимы разрешений приложения ChatGPT: `Always ask`, `Any changes`, `Important
actions`, `Never ask`. Обычно именно `Important actions` объясняет
периодические диалоги: чтение и малорисковые операции могут выполняться
автоматически, а более важные действия требуют подтверждения. Если интерфейс
workspace это позволяет и вы осознанно доверяете локальному DevMCP, для этого
приложения можно выбрать `Never ask`. В управляемых Business/Enterprise/Edu
workspace постоянные разрешения приложения могут задаваться администратором.

`Never ask` отключает обычные подтверждения ChatGPT для приложения, поэтому
используйте его только с доверенным MCP-сервером. Особенно рискованные действия
ChatGPT всё равно может заблокировать. DevMCP не может программно отключить
диалоги подтверждения ChatGPT.

## Возможности и права

Канонические режимы — PLAN и BUILD. PLAN read-only. BUILD используется по
умолчанию и выполняет процессы напрямую как текущий OS-user с обычными
filesystem/environment/network permissions. Выбранный проект задаёт coding
context и scope high-level Git tools, но не является filesystem security root.

Legacy `permission_mode=safe|trusted|dangerous` остаётся только ingress adapter:
`safe -> PLAN`, `trusted` и `dangerous -> BUILD`. Transaction/external executor
изоляция может существовать отдельно, но не является обычной BUILD-моделью.

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
