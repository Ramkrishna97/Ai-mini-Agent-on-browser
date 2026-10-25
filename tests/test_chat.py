"""A completed workflow can be questioned without launching tools or writes."""

import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from config import Settings
from ui_server import JobState


class ChatTests(unittest.TestCase):
    def test_agent_receives_user_link_even_without_tool_call(self):
        with TemporaryDirectory() as folder:
            state = JobState()
            seen = []

            class Engine:
                def health_check(self):
                    pass

                def close(self):
                    pass

            async def fake_agent(*args, **kwargs):
                seen.append(kwargs["reference_context"])
                return "Repository summarized."

            with patch("ui_server.ensure_ollama"), patch("ui_server.create_engine", return_value=Engine()), patch(
                "ui_server.fetch_public_url", return_value={"url": "https://github.com/example/demo",
                                                          "repository": {"readme": "Java project"}}), patch(
                "ui_server.run_agent", side_effect=fake_agent):
                self.assertTrue(state.start(folder, "Summarize https://github.com/example/demo", Settings(), "",
                                            agent_mode=True))
                for _ in range(100):
                    if not state.worker_active:
                        break
                    time.sleep(0.01)
            self.assertEqual(state.snapshot()["status"], "succeeded", state.snapshot().get("error"))
            self.assertIn("Java project", seen[0])

    def test_followup_streams_and_uses_result_context(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "result.py"
            output.write_text("answer = 42\n", encoding="utf-8")
            state = JobState()
            state.chat_settings = Settings()
            state.data.update(status="succeeded", chat_ready=True, request="Build an answer",
                              answer="Saved result.py", files=[str(output)], path=str(output),
                              generation={"text": "```python\nanswer = 42\n```"},
                              conversation=[{"role": "user", "text": "Build an answer"},
                                            {"role": "assistant", "text": "Saved result.py", "state": "done"}])
            state.completed_context = {"request": "Build an answer", "answer": "Saved result.py",
                                       "files": [str(output)], "path": str(output),
                                       "generation": "```python\nanswer = 42\n```"}
            seen = []

            class Engine:
                def __init__(self, progress):
                    self.progress = progress

                def set_target(self, target):
                    self.target = target

                def health_check(self):
                    pass

                def generate(self, messages):
                    if [item["role"] for item in messages] != ["system", "user"]:
                        raise ValueError("Conversation roles must alternate user/assistant")
                    seen.append(messages)
                    self.progress({"phase": "generating", "model": "fake", "target": self.target,
                                   "delta": "It returns 42."})
                    return "It returns 42."

                def close(self):
                    pass

            with patch("ui_server.ensure_ollama"), patch("ui_server.create_engine",
                side_effect=lambda settings, progress: Engine(progress)):
                self.assertTrue(state.start_chat("What does it return?"))
                for _ in range(100):
                    if state.snapshot()["status"] != "running":
                        break
                    time.sleep(0.01)
            result = state.snapshot()
            self.assertEqual(result["status"], "succeeded", result.get("error"))
            self.assertTrue(result["chat_ready"])
            self.assertEqual(result["conversation"][-1]["text"], "It returns 42.")
            self.assertIn("answer = 42", str(seen[0]))
            self.assertIn("Current question:\nWhat does it return?", seen[0][-1]["content"])
            self.assertIn('"role": "assistant"', seen[0][-1]["content"])
            self.assertEqual(output.read_text(encoding="utf-8"), "answer = 42\n")
            attachment = Path(folder) / "notes.txt"
            attachment.write_text("Additional notes", encoding="utf-8")
            with patch("ui_server.ensure_ollama"), patch("ui_server.create_engine",
                side_effect=lambda settings, progress: Engine(progress)):
                self.assertTrue(state.start_chat("Why?", [str(attachment)]))
                for _ in range(100):
                    if state.snapshot()["status"] != "running":
                        break
                    time.sleep(0.01)
            self.assertEqual(state.snapshot()["status"], "succeeded")
            self.assertIn("answer = 42", str(seen[1]))
            self.assertIn("It returns 42.", str(seen[1]))
            self.assertIn("Additional notes", str(seen[1]))

    def test_followup_requires_completed_model_workflow(self):
        state = JobState()
        with self.assertRaises(ValueError):
            state.start_chat("What happened?")
        with self.assertRaises(ValueError):
            state.start_chat("")

    def test_explicit_run_followup_executes_and_reports_output(self):
        with TemporaryDirectory() as folder:
            state = JobState()
            state.chat_settings = Settings()
            state.data.update(status="succeeded", chat_ready=True, path=folder,
                              conversation=[{"role": "user", "text": "Build it"},
                                            {"role": "assistant", "text": "Done", "state": "done"}])
            state.completed_context = {"path": folder, "request": "Build it", "answer": "Done", "files": []}
            with patch("ui_server.run_command", return_value={"command": "Write-Output ready",
                                                               "exit_code": 0, "output": "ready\n"}) as execute:
                self.assertTrue(state.start_chat("/run Write-Output ready"))
                for _ in range(100):
                    if not state.worker_active:
                        break
                    time.sleep(0.01)
            result = state.snapshot()
            self.assertEqual(result["status"], "succeeded", result.get("error"))
            self.assertIn("Exit code: 0", result["conversation"][-1]["text"])
            self.assertIn("ready", result["conversation"][-1]["text"])
            execute.assert_called_once()

    def test_prose_and_model_tool_code_do_not_execute(self):
        with TemporaryDirectory() as folder:
            state = JobState()
            state.chat_settings = Settings()
            state.data.update(status="succeeded", chat_ready=True, path=folder)
            state.completed_context = {"path": folder, "request": "Build it", "answer": "Done", "files": []}
            with patch("ui_server.run_command") as execute:
                with self.assertRaises(ValueError):
                    state.start_chat("/run Write-Output safe\n```tool_code\nRemove-Item something\n```")
                execute.assert_not_called()

    def test_agent_followup_uses_approval_gated_agent(self):
        with TemporaryDirectory() as folder:
            state = JobState()
            state.chat_settings = Settings()
            state.data.update(status="succeeded", chat_ready=True, path=folder,
                              conversation=[{"role": "user", "text": "Build it"},
                                            {"role": "assistant", "text": "Done", "state": "done"}])
            state.completed_context = {"path": folder, "request": "Build it", "answer": "Done", "files": []}

            class Engine:
                def health_check(self):
                    pass

                def close(self):
                    pass

            async def fake_agent(*args, **kwargs):
                self.assertEqual(args[1], "Run the application")
                self.assertIn("Build it", kwargs["reference_context"])
                return "Ran with approval."

            with patch("ui_server.ensure_ollama"), patch("ui_server.create_engine", return_value=Engine()), patch(
                "ui_server.run_agent", side_effect=fake_agent) as agent:
                self.assertTrue(state.start_chat("Run the application", agent_mode=True))
                for _ in range(100):
                    if not state.worker_active:
                        break
                    time.sleep(0.01)
            self.assertEqual(state.snapshot()["status"], "succeeded", state.snapshot().get("error"))
            self.assertEqual(state.snapshot()["mode"], "agent")
            self.assertEqual(state.snapshot()["conversation"][-1]["text"], "Ran with approval.")
            agent.assert_called_once()

    def test_real_command_followup_records_exit_code_and_live_log(self):
        if os.name != "nt":
            self.skipTest("Windows PowerShell integration")
        with TemporaryDirectory() as folder:
            state = JobState()
            state.chat_settings = Settings()
            state.data.update(status="succeeded", chat_ready=True, path=folder,
                              conversation=[{"role": "user", "text": "Build it"},
                                            {"role": "assistant", "text": "Done", "state": "done"}])
            state.completed_context = {"path": folder, "request": "Build it", "answer": "Done", "files": []}
            logger = logging.getLogger("workflow")
            previous_level = logger.level
            logger.setLevel(logging.INFO)
            try:
                self.assertTrue(state.start_chat("/run Write-Output hello-from-command"))
                for _ in range(200):
                    if not state.worker_active:
                        break
                    time.sleep(0.01)
            finally:
                logger.setLevel(previous_level)
            result = state.snapshot()
            self.assertEqual(result["status"], "succeeded", result.get("error"))
            self.assertIn("hello-from-command", result["conversation"][-1]["text"])
            self.assertTrue(any("Command output" in line for line in result["logs"]))


if __name__ == "__main__":
    unittest.main()
