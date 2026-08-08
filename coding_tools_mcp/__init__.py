"""DevMCP Runtime implementation package."""

from importlib.metadata import PackageNotFoundError, version


try:
    # pyproject.toml is the sole release-version source. Installed metadata is
    # also available to the CLI and server without duplicating that value.
    __version__ = version("devmcp-runtime")
except PackageNotFoundError:  # pragma: no cover - only an unpackaged source tree
    __version__ = "0+unknown"
