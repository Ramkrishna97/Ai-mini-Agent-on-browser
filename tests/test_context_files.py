"""Read-only reference files remain separate from output targets."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from config import Settings
from context_files import read_context, validate_context_paths
from workflow import run_workflow


class ContextFileTests(unittest.TestCase):
    def test_multiple_text_types_are_read_without_editing(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = root / "notes.md", root / "data.csv"
            first.write_text("Design note", encoding="utf-8")
            second.write_text("name,value\na,1", encoding="utf-8")
            context = read_context([str(first), str(second)])
            self.assertIn("Design note", context)
            self.assertIn("name,value", context)
            self.assertEqual(first.read_text(encoding="utf-8"), "Design note")
            self.assertFalse(list(root.glob("*.bak")))

    def test_unsupported_binary_duplicate_and_excess_are_rejected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "image.png"
            image.write_bytes(b"image")
            text = root / "note.txt"
            text.write_text("hello", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_context_paths([str(image)])
            with self.assertRaises(ValueError):
                validate_context_paths([str(text), str(text)])
            with self.assertRaises(ValueError):
                validate_context_paths([str(text)] * 9)

    def test_docx_text_is_extracted(self):
        from docx import Document

        with TemporaryDirectory() as folder:
            path = Path(folder) / "brief.docx"
            document = Document()
            document.add_paragraph("Document brief")
            document.save(path)
            self.assertIn("Document brief", read_context([str(path)]))

    def test_references_reach_generation_without_becoming_targets(self):
        class Engine:
            def health_check(self):
                pass

            def generate(self, messages, **kwargs):
                self.messages = messages
                return "```python\nx = 1\n```"

        with TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / "requirements.txt"
            reference.write_text("Example context", encoding="utf-8")
            engine = Engine()
            output = root / "result.py"
            result = run_workflow(str(output), "Write x = 1", Settings(), engine=engine,
                                  reference_context=read_context([str(reference)]))
            self.assertEqual(result.files, [str(output)])
            self.assertIn("Example context", str(engine.messages))
            self.assertEqual(reference.read_text(encoding="utf-8"), "Example context")


if __name__ == "__main__":
    unittest.main()
