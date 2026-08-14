from __future__ import annotations
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
        self.assertIn(
            "package.json", [f["path"] for f in payload.get("affected_files", [])]
        )

        # Verify it didn't actually change the file
        content = (self.workspace.root / "package.json").read_text()
        self.assertIn('"version": "0.0.0"', content)

    def test_5000_line_byte_preservation(self) -> None:
        test_file = self.workspace.root / "5000_lines.txt"
        original_lines = [
            f"Line {i}: Some content that must be perfectly preserved."
            for i in range(5000)
        ]
        test_file.write_bytes(("\n".join(original_lines) + "\n").encode())

        patch = """*** Begin Patch
*** Update File: 5000_lines.txt
@@
 Line 99: Some content that must be perfectly preserved.
-Line 100: Some content that must be perfectly preserved.
+Line 100: MODIFIED REGION ONE.
 Line 101: Some content that must be perfectly preserved.
@@
 Line 2499: Some content that must be perfectly preserved.
-Line 2500: Some content that must be perfectly preserved.
+Line 2500: MODIFIED REGION TWO.
 Line 2501: Some content that must be perfectly preserved.
@@
 Line 3999: Some content that must be perfectly preserved.
-Line 4000: Some content that must be perfectly preserved.
+Line 4000: MODIFIED REGION THREE.
 Line 4001: Some content that must be perfectly preserved.
*** End Patch
"""
        self.assert_tool_success(self.client.call_tool("apply_patch", {"patch": patch}))

        modified_lines = test_file.read_text().splitlines()
        self.assertEqual(modified_lines[100], "Line 100: MODIFIED REGION ONE.")
        self.assertEqual(modified_lines[2500], "Line 2500: MODIFIED REGION TWO.")
        self.assertEqual(modified_lines[4000], "Line 4000: MODIFIED REGION THREE.")
        for index, value in enumerate(modified_lines):
            if index not in {100, 2500, 4000}:
                self.assertEqual(
                    value, original_lines[index], f"unrelated line {index} changed"
                )

        destructive_patch = (
            "*** Begin Patch\n*** Update File: 5000_lines.txt\n@@\n"
            + "".join(f"-{line}\n" for line in modified_lines)
            + "".join(f"+{line}\n" for line in original_lines[:100])
            + "*** End Patch\n"
        )
        preview = self.client.call_tool("preview_patch", {"patch": destructive_patch})
        preview_payload = self.assert_tool_success(preview)
        self.assertGreater(preview_payload.get("removals", 0), 4800)
        res = self.client.call_tool("apply_patch", {"patch": destructive_patch})
        self.assertTrue(res.get("structuredContent", {}).get("clean"))
