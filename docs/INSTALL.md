# Installation

Linux is the supported beta platform. Install Python 3.11+, Git, bubblewrap,
and optionally the official `tunnel-client` binary. Then:

```bash
uv tool install git+https://github.com/GolovIaroslav/devmcp-runtime.git
devmcp setup --workspace /absolute/path/to/repository
```

For a checkout:

```bash
uv pip install -e '.[dev]'
devmcp setup --workspace /absolute/path/to/repository
```

`devmcp service install` writes only systemd user units. It does not use sudo,
enable linger, or delete user data.
