from __future__ import annotations
import os
from tests.compliance.test_support import ComplianceTestCase

class PatchSafetyTests(ComplianceTestCase):
    fixture_name = "tiny-js-project"

    def test_preview_patch_is_dry_run(self) -> None:
        patch = """*** Begin Patch
*** Update File: package.json
@@ -2,3 +2,3 @@
   "name": "tiny-js-project",
-  "version": "0.0.0",
+  "version": "1.0.1",
   "type": "module",
*** End Patch
"""
        preview_res = self.client.call_tool("preview_patch", {"patch": patch})
        payload = self.assert_tool_success(preview_res)
        self.assertIn("package.json", [f["path"] for f in payload.get("affected_files", [])])
        
        # Verify it didn't actually change the file
        content = (self.workspace.root / "package.json").read_text()
        self.assertIn('"version": "0.0.0"', content)

    def test_5000_line_byte_preservation(self) -> None:
        # Create a 5000-line file
        test_file = self.workspace.root / "5000_lines.txt"
        original_lines = [f"Line {i}: Some content that must be perfectly preserved." for i in range(5000)]
        test_file.write_text("\n".join(original_lines) + "\n")
        
        # Patch the middle of the file
        patch = """*** Begin Patch
*** Update File: 5000_lines.txt
@@ -2499,3 +2499,3 @@
 Line 2498: Some content that must be perfectly preserved.
-Line 2499: Some content that must be perfectly preserved.
+Line 2499: MODIFIED CONTENT.
 Line 2500: Some content that must be perfectly preserved.
*** End Patch
"""
        res = self.client.call_tool("apply_patch", {"patch": patch})
        self.assert_tool_success(res)
        
        # Verify preservation
        modified_lines = test_file.read_text().splitlines()
        self.assertEqual(len(modified_lines), 5000)
        self.assertEqual(modified_lines[0], "Line 0: Some content that must be perfectly preserved.")
        self.assertEqual(modified_lines[-1], "Line 4999: Some content that must be perfectly preserved.")
        self.assertEqual(modified_lines[2499], "Line 2499: MODIFIED CONTENT.")
