# Права доступа DevMCP Runtime

Права исполнения в runtime определяются один раз при старте единым резолвером:
`resolve_execution_mode()` в `coding_tools_mcp/policy.py`.

## Режимы исполнения (Execution Modes)

Поддерживаются два режима:

```text
--execution-mode plan     # режим чтения (read-only); apply_patch и exec_command запрещены
--execution-mode build    # полный доступ (full-access); права текущего пользователя ОС (по умолчанию)
```

Примеры:

```bash
devmcp service update --execution-mode build   # явный запуск в режиме BUILD
devmcp service update                          # BUILD используется по умолчанию
```

## Адаптеры совместимости (Ingress Adapters)

Старый флаг `--permission-mode` сохраняется в качестве адаптера входного формата:

| `--permission-mode` | Отображается в |
|---|---|
| `safe` | PLAN (read-only) |
| `trusted` | BUILD (full-access) |
| `dangerous` | BUILD (full-access) |

Эти значения разрешаются только при запуске. Они не влияют на поведение runtime после разрешения режима.

## Доступ в режиме BUILD

В режиме BUILD сервер работает с правами текущего пользователя ОС. Никаких внутрипроцессных проверок approval, списков запрещенных команд или матриц профилей не применяется. Проверка путей (границы workspace, отказ от симлинков, проверка NUL/traversal) сохраняется для прямых файловых инструментов.

## Доступ в режиме PLAN

В режиме PLAN:
- `read_file`, `read_files`, `list_dir`, `list_files`, `search_text`, `view_image`, `preview_patch`, `git_status`, `git_diff`, `git_log`, `git_show`, `git_blame` — разрешены
- `apply_patch` — запрещён (`PERMISSION_REQUIRED`)
- `exec_command` — запрещён (`PERMISSION_REQUIRED`)
- Все остальные read-only инструменты — разрешены

## Инварианты безопасности

Применяются во всех режимах:
- Прямые файловые инструменты отклоняют абсолютные пути, `..` traversal, NUL-байты и симлинки, выходящие за пределы workspace.
- Проверки `exec_command` выполняются на уровне ОС.
- Удаленный доступ требует аутентификации по Bearer или OAuth.
