#!/usr/bin/env python3
"""Validate release tag, package versions, and release-note coverage."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def validate_release(root: Path, tag: str) -> str:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject["project"]["version"]

    expected_tag = f"v{project_version}"
    if tag != expected_tag:
        raise SystemExit(f"release tag {tag!r} does not match {expected_tag!r}")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(project_version)} - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.MULTILINE):
        raise SystemExit(f"CHANGELOG.md has no dated {project_version} release heading")
    if re.search(r"^## Unreleased\s*$", changelog, re.MULTILINE):
        raise SystemExit("CHANGELOG.md still contains an Unreleased section")

    return project_version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True, help="Release tag, for example v0.2.0")
    args = parser.parse_args()

    project_version = validate_release(ROOT, args.tag)
    print(f"Release metadata OK: DevMCP Runtime {project_version} ({args.tag}); no registry publishing is configured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
