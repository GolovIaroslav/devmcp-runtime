import os
from pathlib import Path

from calc import add


def test_add():
    assert add(2, 3) == 5


def test_registered_tests_use_authoritative_workspace():
    authoritative = os.environ.get("AUTHORITATIVE_WORKSPACE")
    if authoritative:
        root = Path(authoritative).resolve()
        assert root == Path.cwd().resolve()
        probe = root / ".devmcp-workspace-write-probe"
        probe.write_text("workspace-write\n", encoding="utf-8")
        assert probe.read_text(encoding="utf-8") == "workspace-write\n"
        probe.unlink()
