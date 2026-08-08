# Development

```bash
uv pip install -e '.[dev]'
make lint
make typecheck
make test
make dogfood-smoke
```

Linux+bwrap is the authoritative security environment. Focused unit and
compliance tests can run without every optional toolchain; missing toolchains
must produce a clean diagnostic, not `INTERNAL_ERROR`.

Keep model-facing schemas stable. Put new ecosystem commands into the task
registry and policy data. Never add secrets or local runtime artifacts to Git.
