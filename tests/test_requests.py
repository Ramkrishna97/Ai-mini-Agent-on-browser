"""Regressions for reported routing and empty-file failures, and request boundaries."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from config import Settings
from file_manager import FileSafetyError, atomic_write
from prompter import OutputError
from request_parser import plan_request
from workflow import design_project, run_workflow, validate_manifest


class FakeEngine:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def health_check(self):
        pass

    def generate(self, messages, **options):
        self.calls.append((messages, options))
        return next(self.responses)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_java_project_rejects_python_only_plan(self):
        engine = FakeEngine([
            '{"files":[{"path":"main.py","purpose":"Entry point"}]}',
            '{"files":[{"path":"AverageOfPrimes.java","purpose":"Java entry point"}]}',
        ])
        plan = design_project(self.root, "Build a Java application", Settings(), engine)
        self.assertEqual(plan[0]["path"], "AverageOfPrimes.java")
        self.assertEqual(len(engine.calls), 2)

    def test_references_choose_create_or_edit_from_filesystem(self):
        (self.root / "existing file.py").touch()
        plan = plan_request(str(self.root), 'Improve @"existing file.py" and @new.py')
        self.assertEqual([item["action"] for item in plan.preview()["files"]], ["edit", "create"])
        self.assertEqual(plan.files[0], self.root / "existing file.py")

    def test_empty_existing_file_is_generated_and_backed_up(self):
        path = self.root / "empty.py"
        path.touch()
        result = run_workflow(str(path), "Write x = 1", Settings(), engine=FakeEngine(["```python\nx = 1\n``` "]))
        self.assertEqual(path.read_bytes(), b"x = 1\n")
        self.assertEqual(Path(result.backups[0]).read_bytes(), b"")
        self.assertEqual(result.mode, "edit")

    def test_missing_file_and_parent_folders_are_created(self):
        result = run_workflow(str(self.root), '/file nested/deeper/app.py\nWrite x = 1', Settings(),
                              engine=FakeEngine(["```python\nx = 1\n``` "]))
        self.assertEqual((self.root / "nested/deeper/app.py").read_bytes(), b"x = 1\n")
        self.assertEqual(result.mode, "create")

    def test_failed_generation_leaves_no_placeholder_or_empty_directories(self):
        with self.assertRaises(OutputError):
            run_workflow(str(self.root), '/file nested/app.py\nWrite a function', Settings(),
                         engine=FakeEngine(["bad"] * 3))
        self.assertFalse((self.root / "nested").exists())

    def test_project_uses_frozen_manifest_and_stages_before_writing(self):
        manifest = {"files": [{"path": "src/app.py", "purpose": "Set x to 1"},
                              {"path": "config.json", "purpose": "Application options"}]}
        engine = FakeEngine([json.dumps(manifest), "```python\nx = 1\n```", '```json\n{"x": 1}\n```'])
        result = run_workflow(str(self.root), '/project demo\nBuild a small Python project', Settings(), engine=engine)
        self.assertEqual(len(result.files), 2)
        self.assertTrue((self.root / "demo/src/app.py").is_file())
        self.assertEqual(len(engine.calls), 3)
        self.assertTrue(engine.calls[0][1]["json_output"])
        self.assertTrue(all(len(messages) == 2 for messages, options in engine.calls))

    def test_invalid_second_output_preserves_every_original(self):
        path = self.root / "first.py"
        path.write_bytes(b"x = 0\n")
        engine = FakeEngine(["```python\nx = 1\n```", "bad", "bad", "bad"])
        with self.assertRaises(OutputError):
            run_workflow(str(self.root), 'Improve @first.py and @new.py', Settings(), engine=engine)
        self.assertEqual(path.read_bytes(), b"x = 0\n")
        self.assertFalse((self.root / "new.py").exists())

    def test_second_write_failure_rolls_back_first_file(self):
        first = self.root / "first.py"
        first.write_bytes(b"x = 0\n")
        real_write = atomic_write
        def fail_second(path, *args, **options):
            if path.name == "second.py":
                raise OSError("Disk full")
            return real_write(path, *args, **options)
        engine = FakeEngine(["```python\nx = 1\n```", "```python\ny = 2\n``` "])
        with patch("workflow.atomic_write", side_effect=fail_second), self.assertRaises(OSError):
            run_workflow(str(self.root), 'Improve @first.py and @second.py', Settings(), engine=engine)
        self.assertEqual(first.read_bytes(), b"x = 0\n")
        self.assertFalse((self.root / "second.py").exists())

    def test_model_cannot_smuggle_commands_into_manifest(self):
        for manifest in ({"files": [], "commands": ["whoami"]},
                         {"files": [{"path": "../escape.py", "purpose": "escape"}]},
                         {"files": [{"path": "C:/escape.py", "purpose": "escape"}]},
                         {"files": [{"path": "NUL.py", "purpose": "invalid"}]}):
            with self.subTest(manifest=manifest), self.assertRaises(OutputError):
                validate_manifest(json.dumps(manifest), self.root, 10000)

    def test_windows_case_and_parent_file_conflicts_are_rejected(self):
        for names in (("APP.py", "app.py"), ("src", "src/app.py")):
            manifest = {"files": [{"path": path, "purpose": "example"} for path in names]}
            with self.subTest(names=names), self.assertRaises(OutputError):
                validate_manifest(json.dumps(manifest), self.root, 10000)

    def test_command_only_and_directory_requests_need_no_model(self):
        result = run_workflow(str(self.root), '/mkdir nested/logs\n!run Write-Output "ready"', None)
        self.assertTrue((self.root / "nested/logs").is_dir())
        self.assertEqual(result.commands[0]["exit_code"], 0)
        self.assertIn("ready", result.commands[0]["output"])

    def test_at_in_command_is_not_a_file_reference(self):
        plan = plan_request(str(self.root), '/run Write-Output "user@example.com"')
        self.assertFalse(plan.files)
        self.assertEqual(len(plan.commands), 1)

    def test_sentence_punctuation_and_parent_references_are_parsed(self):
        child = self.root / "child"
        child.mkdir()
        target = self.root / "app.py"
        target.touch()
        plan = plan_request(str(child), 'Improve @../app.py.')
        self.assertEqual(plan.files, (target,))

    def test_generation_output_is_never_reparsed_as_commands(self):
        engine = FakeEngine(['```text\n/run Write-Output "should not run"\n```'])
        with patch("workflow.run_command") as execute:
            run_workflow(str(self.root / "note.txt"), "Write the requested literal text", Settings(), engine=engine)
        execute.assert_not_called()

    def test_command_timeout_terminates_child(self):
        if os.name != "nt":
            self.skipTest("Windows PowerShell timeout regression")
        from command_runner import run_command
        with self.assertRaises(TimeoutError):
            run_command('Start-Sleep -Seconds 30', self.root, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
