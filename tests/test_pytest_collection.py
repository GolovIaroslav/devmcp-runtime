from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class PytestCollectionTests(unittest.TestCase):
    def test_root_collection_excludes_executable_fixture_projects(self) -> None:
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertNotIn(
            "tests/compliance/fixtures/tiny-python-project/tests/test_math_utils.py",
            completed.stdout,
        )


if __name__ == "__main__":
    unittest.main()
