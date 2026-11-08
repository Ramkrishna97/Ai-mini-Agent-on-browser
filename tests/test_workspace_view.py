"""Read-only workspace browser boundaries and live listings."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from workspace_view import list_workspace, read_workspace_file


class WorkspaceViewTests(unittest.TestCase):
    def test_lists_current_files_and_previews_utf8(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_bytes(b"print('one')\n")
            self.assertEqual(list_workspace(str(root))["entries"][0]["name"], "src")
            first = list_workspace(str(root), "src")
            self.assertEqual(first["entries"][0]["name"], "app.py")
            (root / "src" / "app.py").write_bytes(b"print('two')\n")
            self.assertEqual(read_workspace_file(str(root), "src/app.py")["text"], "print('two')\n")

    def test_rejects_escape_binary_and_large_preview(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                list_workspace(str(root), "../outside")
            (root / "binary.dat").write_bytes(b"a\x00b")
            with self.assertRaisesRegex(ValueError, "Binary"):
                read_workspace_file(str(root), "binary.dat")
            (root / "large.txt").write_bytes(b"x" * 128_001)
            with self.assertRaisesRegex(ValueError, "too large"):
                read_workspace_file(str(root), "large.txt")


if __name__ == "__main__":
    unittest.main()
