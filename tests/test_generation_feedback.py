"""Regressions for the reported blank output, prompt echoes and missing suggestions."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from config import OllamaEngine, Settings, WorkflowCancelled
from main import generate_content
from project_templates import project_template, validate_file_contract
from prompter import OutputError, reject_prompt_echo
from request_parser import plan_request
from suggestions import suggestions


class FeedbackTests(unittest.TestCase):
    def test_bare_wsl_command_routes_without_any_model(self):
        # Parse only: never install WSL as part of verification.
        with TemporaryDirectory() as root:
            plan = plan_request(root, "wsl --install")
        self.assertEqual(plan.commands, ("wsl --install",))
        self.assertFalse(plan.needs_model)

    def test_slash_provides_reserved_words_and_not_a_project(self):
        with TemporaryDirectory() as root:
            self.assertEqual({item["label"] for item in suggestions(root, "command", "/")},
                             {"/file", "/project", "/mkdir", "/run"})
            with self.assertRaises(ValueError):
                plan_request(root, "/")

    def test_reference_suggestions_find_files_and_subfolders(self):
        with TemporaryDirectory() as root:
            folder = Path(root)
            (folder / "source").mkdir()
            (folder / "source/app.py").touch()
            self.assertEqual(suggestions(root, "file", "sou")[0]["insert"], "source/")
            self.assertEqual(suggestions(root, "file", "source/a")[0]["insert"], "source/app.py")

    def test_prompt_envelope_is_not_accepted_as_a_generated_json_file(self):
        with self.assertRaises(OutputError):
            reject_prompt_echo('{"instruction":"CRUD app", "chunk":1,"source":""}', "json")

    def test_spring_react_scaffold_requires_multiple_file_types(self):
        template = project_template("create project for CRUD using Java, springboot and React")
        extensions = {Path(item["path"]).suffix for item in template}
        self.assertTrue({".java", ".xml", ".jsx", ".json", ".html", ".css"} <= extensions)
        with self.assertRaises(OutputError):
            validate_file_contract("<root><application/></root>", "backend/pom.xml")

    def test_ui_receives_partial_content_before_generation_finishes(self):
        events = []
        client = Mock()
        def stream():
            yield SimpleNamespace(message=SimpleNamespace(content="```python\nx = "), done=False)
            self.assertTrue(any(event.get("delta") for event in events))
            yield SimpleNamespace(message=SimpleNamespace(content="1\n```"), done=True,
                                  done_reason="stop", eval_count=8, eval_duration=100000000)
        client.chat.return_value = stream()
        engine = OllamaEngine(Settings(), client, progress=events.append)
        self.assertEqual(engine.generate([{"role": "user", "content": "Write x = 1"}]), "```python\nx = 1\n```")
        self.assertTrue(client.chat.call_args.kwargs["stream"])
        self.assertEqual(events[-1]["generated_tokens"], 8)

    def test_rejection_reason_is_sent_to_bounded_retry(self):
        engine = Mock()
        engine.generate.side_effect = ["```python\nx =\n```", "```python\nx = 1\n```"]
        generate_content("", "Write x = 1", "python", Settings(), engine)
        self.assertIn("Previous rejection:", engine.generate.call_args_list[1].args[0][0]["content"])

    def test_cancel_interrupts_the_stream_without_returning_partial_code(self):
        client = Mock()
        client.chat.return_value = iter([SimpleNamespace(message=SimpleNamespace(content="partial"), done=False)])
        def progress(event):
            if event.get("delta"):
                raise WorkflowCancelled("Stop requested")
        with self.assertRaises(WorkflowCancelled):
            OllamaEngine(Settings(), client, progress=progress).generate([])

    def test_agent_stream_emits_live_content(self):
        client = Mock()
        client.chat.return_value = iter([
            SimpleNamespace(message=SimpleNamespace(content="Work"), done=False),
            SimpleNamespace(message=SimpleNamespace(content=" done"), done=True),
        ])
        events = []
        response = OllamaEngine(Settings(), client, progress=events.append).chat_agent([], [])
        self.assertEqual(response.message.content, "Work done")
        self.assertEqual([event["delta"] for event in events if event.get("delta")], ["Work", " done"])
        self.assertTrue(client.chat.call_args.kwargs["stream"])


if __name__ == "__main__":
    unittest.main()
