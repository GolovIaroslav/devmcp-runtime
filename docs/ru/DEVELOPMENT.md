# Разработка

```bash
uv pip install -e '.[dev]'
make lint
make typecheck
make test
make dogfood-smoke
```

Linux+bwrap — authoritative security environment. Отсутствующий toolchain
должен давать понятную диагностику, а не `INTERNAL_ERROR`. Новые команды
добавляйте в task registry и policy data, не раздувайте MCP tool catalog.
