import os
import socket
from pathlib import Path

from calc import add


def test_add():
    assert add(2, 3) == 5


def test_sandbox_cannot_reach_network_or_authoritative_workspace():
    try:
        socket.create_connection(("1.1.1.1", 80), timeout=0.2)
    except OSError:
        pass
    else:
        raise AssertionError("registered tests must run without network access")

    authoritative = os.environ.get("AUTHORITATIVE_WORKSPACE")
    if authoritative:
        target = Path(authoritative) / "calc.py"
        try:
            target.write_text("sandbox must not write authoritative files\n", encoding="utf-8")
        except OSError:
            pass
        else:
            raise AssertionError("sandbox child modified the authoritative workspace")
