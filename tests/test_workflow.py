"""Offline safety regression tests; no model or network is required."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from config import ModelError, OllamaEngine, Settings
from file_manager import (FileSafetyError, atomic_write, create_backup, read_snapshot,
                          rollback, workflow_lock)
from main import transform
from prompter import OutputError, extract_code, split_chunks


class FakeEngine:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def health_check(self):
        pass

    def generate(self, messages):
        self.calls.append(messages)
        return next(self.responses)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sample.py"
        self.original = b"x = 1\r\n"
        self.path.write_bytes(self.original)

    def test_success_preserves_bom_newlines_and_backup(self):
        self.path.write_bytes(b"\xef\xbb\xbf" + self.original)
        engine = FakeEngine(["```python\nx = 2\n```"])
        backup = transform(str(self.path), "Set x to 2", Settings(), engine)
        self.assertEqual(self.path.read_bytes(), b"\xef\xbb\xbfx = 2\r\n")
        self.assertEqual(backup.read_bytes(), b"\xef\xbb\xbf" + self.original)
        self.assertFalse(list(self.path.parent.glob("*.workflow.lock")))

    def test_retry_is_bounded_and_stateless(self):
        engine = FakeEngine(["bad", "```python\nx =\n```", "```python\nx = 3\n```"])
        transform(str(self.path), "Set x to 3", Settings(), engine)
        self.assertEqual(len(engine.calls), 3)
        self.assertTrue(all(len(call) == 2 for call in engine.calls))
        self.assertIn("STRICT FORMAT REMINDER", engine.calls[1][0]["content"])

    def test_exhaustion_preserves_original(self):
        engine = FakeEngine(["bad"] * 3)
        with self.assertRaises(OutputError):
            transform(str(self.path), "Update", Settings(), engine)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(len(engine.calls), 3)
        self.assertEqual(len(list(self.path.parent.glob("*.bak"))), 1)

    def test_atomic_replace_failure_preserves_original(self):
        engine = FakeEngine(["```python\nx = 2\n```"])
        with patch("file_manager.os.replace", side_effect=PermissionError("File is open")):
            with self.assertRaises(PermissionError):
                transform(str(self.path), "Update", Settings(), engine)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_partial_staging_write_failure_preserves_original(self):
        with patch("file_manager.os.fsync", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                atomic_write(self.path, b"replacement", 0o600)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_rollback_restores_committed_candidate(self):
        snapshot = read_snapshot(self.path, 100)
        backup = create_backup(snapshot)
        self.path.write_bytes(b"x = 2\r\n")
        rollback(snapshot, backup, b"x = 2\r\n")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_concurrent_edit_is_never_overwritten(self):
        engine = FakeEngine(["```python\nx = 2\n```"])
        original_generate = engine.generate

        def edit_during_generation(messages):
            self.path.write_bytes(b"external = True\n")
            return original_generate(messages)

        engine.generate = edit_during_generation
        with self.assertRaises(FileSafetyError):
            transform(str(self.path), "Update", Settings(), engine)
        self.assertEqual(self.path.read_bytes(), b"external = True\n")

    def test_lock_rejects_second_workflow(self):
        with workflow_lock(self.path):
            with self.assertRaises(FileSafetyError):
                with workflow_lock(self.path):
                    self.fail("Should not acquire second lock")
            self.assertTrue(self.path.with_name("sample.py.workflow.lock").exists())

    def test_chunks_reassemble_before_syntax_check(self):
        self.path.write_bytes(b"def f():\n    return 1\n")
        engine = FakeEngine(["```python\ndef f():\n```", "```python\n    return 2\n```"])
        transform(str(self.path), "Return 2", Settings(chunk_chars=13), engine)
        self.assertEqual(self.path.read_bytes(), b"def f():\n    return 2\n")

    def test_preserves_missing_final_newline(self):
        self.path.write_bytes(b"x = 1")
        transform(str(self.path), "Update", Settings(), FakeEngine(["```python\nx = 2\n``` "]))
        self.assertEqual(self.path.read_bytes(), b"x = 2")

    def test_rejects_multiple_fences_prose_and_wrong_label(self):
        for response in ("text\n```python\nx=1\n```", "```java\nx=1\n```",
                         "```python\nx=1\n```\n```python\nx=2\n```", "```python\n\n```"):
            with self.subTest(response=response), self.assertRaises(OutputError):
                extract_code(response, "python", 1000)

    def test_longer_fences_allow_markdown_source(self):
        self.assertEqual(extract_code("````markdown\n```python\nx=1\n```\n````",
                                      "markdown", 1000), "```python\nx=1\n```")

    def test_limits_and_encoding(self):
        with self.assertRaises(ValueError):
            split_chunks("a very long line", 2, 10)
        with self.assertRaises(ValueError):
            split_chunks("a\nb\nc\n", 2, 2)
        for data in (b"a\r\nb\n", b"\xff", b"\x00", b""):
            self.path.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(FileSafetyError):
                read_snapshot(self.path, 1000)

    def test_health_loads_and_checks_model(self):
        from unittest.mock import Mock
        client = Mock()
        client.list.return_value = SimpleNamespace(models=[SimpleNamespace(model="qwen2.5-coder:7b")])
        client.ps.return_value = client.list.return_value
        OllamaEngine(Settings(), client).health_check()
        client.generate.assert_called_once_with(model="qwen2.5-coder:7b", prompt="", keep_alive="10m")
        client.list.return_value = SimpleNamespace(models=[])
        with self.assertRaises(ModelError):
            OllamaEngine(Settings(), client).health_check()

    def test_model_failure_rolls_back_without_format_retries(self):
        engine = FakeEngine([])
        def fail():
            raise ModelError("Offline")
        engine.health_check = fail
        with self.assertRaises(ModelError):
            transform(str(self.path), "Update", Settings(), engine)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(engine.calls, [])

    def test_client_generation_contract_and_truncation(self):
        from unittest.mock import Mock
        client = Mock()
        client.chat.return_value = SimpleNamespace(
            done_reason="stop", message=SimpleNamespace(content="```python\nx=2\n```"))
        engine = OllamaEngine(Settings(), client)
        messages = [{"role": "user", "content": "bounded input"}]
        self.assertEqual(engine.generate(messages), "```python\nx=2\n```")
        arguments = client.chat.call_args.kwargs
        self.assertEqual(arguments["messages"], messages)
        self.assertFalse(arguments["stream"])
        self.assertNotIn("tools", arguments)
        self.assertEqual(arguments["options"]["num_predict"], 4096)
        client.chat.return_value.done_reason = "length"
        with self.assertRaises(ModelError):
            engine.generate(messages)

    def test_invalid_json_never_written(self):
        self.path = self.path.with_suffix(".json")
        self.path.write_bytes(b'{"valid": true}\n')
        engine = FakeEngine(['```json\n{"invalid": }\n```'] * 3)
        with self.assertRaises(OutputError):
            transform(str(self.path), "Update", Settings(), engine)
        self.assertEqual(self.path.read_bytes(), b'{"valid": true}\n')


if __name__ == "__main__":
    unittest.main()
