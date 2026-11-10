from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from zeroscript_agent.patching import PatchError, apply_file_patch, parse_unified_diff
from zeroscript_agent.security import PathGuard, WorkspaceSecurityError, revision
from zeroscript_agent.workspace import ToolError, WorkspaceRuntime


CONFIG = {
    "workspace": {
        "max_file_bytes": 250000,
        "exclude": [".git", "node_modules", "__pycache__"],
        "secret_patterns": [".env", "*.pem"],
    },
    "runner": {"mode": "disabled", "timeout_ms": 1000},
    "limits": {"max_output_bytes": 10000},
}


class PathGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.guard = PathGuard(self.root, [".git"], [".env", "*.key"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_parent_traversal_is_rejected(self) -> None:
        with self.assertRaises(WorkspaceSecurityError):
            self.guard.resolve("../outside.txt", must_exist=False)

    def test_absolute_path_is_rejected(self) -> None:
        with self.assertRaises(WorkspaceSecurityError):
            self.guard.resolve(str((self.root / "x").resolve()), must_exist=False)

    def test_secret_is_rejected(self) -> None:
        secret = self.root / ".env"
        secret.write_text("TOKEN=secret", encoding="utf-8")
        with self.assertRaises(WorkspaceSecurityError):
            self.guard.assert_readable(secret)

    def test_symlink_escape_is_rejected_when_supported(self) -> None:
        outside = self.root.parent / f"outside-{self.root.name}"
        outside.mkdir(exist_ok=True)
        link = self.root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks require additional privileges on this platform")
        try:
            with self.assertRaises(WorkspaceSecurityError):
                self.guard.resolve("escape/file.txt", must_exist=False)
        finally:
            link.unlink(missing_ok=True)
            outside.rmdir()


class PatchTests(unittest.TestCase):
    def test_parse_and_apply_unified_diff(self) -> None:
        patch = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 value = 1
-print(value)
+print(value + 1)
"""
        parsed = parse_unified_diff(patch)
        self.assertEqual(apply_file_patch("value = 1\nprint(value)\n", parsed[0]), "value = 1\nprint(value + 1)\n")

    def test_mismatched_context_fails(self) -> None:
        patch = """--- a/app.py
+++ b/app.py
@@ -1,1 +1,1 @@
-wrong
+right
"""
        with self.assertRaises(PatchError):
            apply_file_patch("actual\n", parse_unified_diff(patch)[0])


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root.parent / f"data-{self.root.name}"
        self.runtime = WorkspaceRuntime(self.root, CONFIG, self.data)

    def tearDown(self) -> None:
        self.temp.cleanup()
        if self.data.exists():
            import shutil
            shutil.rmtree(self.data)

    def test_read_returns_revision_and_numbered_lines(self) -> None:
        target = self.root / "hello.txt"
        target.write_bytes(b"one\r\ntwo\r\n")
        result = self.runtime.tool_read_file("hello.txt", 2, 2)
        self.assertEqual(result["revision"], revision(target.read_bytes()))
        self.assertEqual(result["newline"], "crlf")
        self.assertIn("2 | two", result["content"])

    def test_replace_lines_requires_current_revision_and_preserves_crlf(self) -> None:
        target = self.root / "hello.txt"
        target.write_bytes(b"one\r\ntwo\r\n")
        rev = revision(target.read_bytes())
        result = self.runtime.tool_replace_lines("hello.txt", 2, 2, "changed", rev)
        self.assertEqual(target.read_bytes(), b"one\r\nchanged\r\n")
        self.assertTrue(result["revision"].startswith("sha256:"))
        with self.assertRaises(ToolError):
            self.runtime.tool_replace_lines("hello.txt", 1, 1, "stale", rev)

    def test_apply_patch_is_all_or_nothing(self) -> None:
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("first\n", encoding="utf-8")
        second.write_text("second\n", encoding="utf-8")
        patch = """--- a/first.txt
+++ b/first.txt
@@ -1,1 +1,1 @@
-first
+changed
--- a/second.txt
+++ b/second.txt
@@ -1,1 +1,1 @@
-not-second
+also changed
"""
        with self.assertRaises(PatchError):
            self.runtime.tool_apply_patch(patch)
        self.assertEqual(first.read_text(encoding="utf-8"), "first\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "second\n")

    def test_apply_patch_can_create_nested_file(self) -> None:
        patch = """--- /dev/null
+++ b/src/new.txt
@@ -0,0 +1,2 @@
+hello
+world
"""
        result = self.runtime.tool_apply_patch(patch)
        self.assertEqual((self.root / "src" / "new.txt").read_text(encoding="utf-8"), "hello\nworld\n")
        self.assertEqual(result["changed_files"], ["src/new.txt"])

    def test_search_skips_secrets(self) -> None:
        (self.root / "normal.txt").write_text("needle", encoding="utf-8")
        (self.root / ".env").write_text("needle=secret", encoding="utf-8")
        result = self.runtime.tool_search_text("needle")
        self.assertEqual([m["path"] for m in result["matches"]], ["normal.txt"])

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_diff_excludes_secret_contents(self) -> None:
        (self.root / "normal.txt").write_text("before\n", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=before\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "normal.txt", ".env"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
            cwd=self.root, check=True,
        )
        (self.root / "normal.txt").write_text("after\n", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=super-secret\n", encoding="utf-8")
        diff = self.runtime.tool_git_diff()
        self.assertIn("after", diff)
        self.assertNotIn("super-secret", diff)
        status = self.runtime.tool_git_status()
        self.assertIn("normal.txt", status)
        self.assertNotIn(".env", status)

    def test_command_always_requests_approval_metadata(self) -> None:
        details = self.runtime.approval_details("run_command", {"argv": ["python", "-V"], "cwd": "."})
        self.assertIsNotNone(details)
        self.assertEqual(details["risk"], "command")
        self.assertEqual(details["command"], "python -V")

    def test_disabled_runner_refuses_direct_execution(self) -> None:
        with self.assertRaises(ToolError):
            self.runtime.tool_run_command(["python", "-V"])


if __name__ == "__main__":
    unittest.main()

