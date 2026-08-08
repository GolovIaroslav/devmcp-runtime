# DevMCP Runtime quickstart

The PyPI package is not published yet. Install the current source directly:

```bash
uv tool install git+https://github.com/GolovIaroslav/test.git
devmcp setup --workspace /absolute/path/to/project --no-tunnel
devmcp doctor
devmcp service install
devmcp start
```

Open the loopback admin UI with `devmcp ui`. Select a policy profile there (or
with `devmcp policy profile balanced`); the runtime reads `config.toml` each
time `devmcp serve` starts, including after `devmcp restart`.

For a source checkout, use `uv pip install -e '.[dev]'`. After trusted
publishing for `devmcp-runtime` has been deliberately configured, the install
command will become `uv tool install devmcp-runtime`.
