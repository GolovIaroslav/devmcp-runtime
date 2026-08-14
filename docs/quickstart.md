# DevMCP Runtime quickstart

The PyPI package is not published yet. Install the current source directly:

```bash
uv tool install git+https://github.com/GolovIaroslav/devmcp-runtime.git
devmcp setup --workspace /absolute/path/to/project --no-tunnel
devmcp doctor
devmcp service install
devmcp start
```

Select an execution mode (`--execution-mode build` for full-access default, or `--execution-mode plan` for read-only confinement); authority resolves once at startup.

For a source checkout, use `uv pip install -e '.[dev]'`. After trusted
publishing for `devmcp-runtime` has been deliberately configured, the install
command will become `uv tool install devmcp-runtime`.
