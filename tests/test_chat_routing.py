"""Chat stays cheap until a user explicitly requests a workspace action."""

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import Request, urlopen

from chat_routing import chat_messages, chat_needs_tools
from config import Settings
from ui_server import JobState, Server, SessionStore


class ChatRoutingTests(unittest.TestCase):
    def test_ide_opt_in_uses_tools_for_action_then_returns_to_light_chat(self):
        async def agent(*args, **kwargs):
            return "Inspected the requested file."

        with TemporaryDirectory() as folder, \
                patch("ui_server.SessionStore", side_effect=lambda: SessionStore(folder)), \
                patch("ui_server.ensure_ollama", return_value="http://localhost:11434"), \
                patch("ui_server.create_engine") as factory, patch("ui_server.run_agent", side_effect=agent) as tools:
            factory.return_value.supports_tools.return_value = False
            factory.return_value.generate.return_value = "Hello"
            with Server(("127.0.0.1", 0)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{server.server_port}"
                try:
                    with urlopen(base + "/api/session") as response:
                        session = json.load(response)
                    headers = {"Content-Type": "application/json", "X-Workflow-Token": session["token"],
                               "X-Session-Id": session["id"]}

                    def send(route, payload):
                        with urlopen(Request(base + route, method="POST", headers=headers,
                                             data=json.dumps(payload).encode())) as response:
                            self.assertEqual(response.status, 202)
                        for _ in range(100):
                            with urlopen(Request(base + "/api/status", headers=headers)) as response:
                                status = json.load(response)
                            if status["status"] != "running":
                                self.assertEqual(status["status"], "succeeded", status.get("error"))
                                return status
                            time.sleep(.01)
                        self.fail("Chat did not finish")

                    common = {"chat_only": True, "lazy_tools": True, "mcp": True, "browser": True}
                    first = send("/api/run", dict(common, path=folder, instruction="Hello", provider="ollama",
                                                  model="test:latest", name=""))
                    self.assertEqual(first["mode"], "chat")
                    self.assertFalse(first["mcp"])
                    tools.assert_not_called()
                    second = send("/api/chat", dict(common, question="Please inspect main.py"))
                    self.assertEqual(second["mode"], "agent")
                    self.assertTrue(second["mcp"])
                    self.assertEqual(tools.call_count, 1)
                    third = send("/api/chat", dict(common, question="Thanks"))
                    self.assertEqual(third["mode"], "chat")
                    self.assertFalse(third["mcp"])
                    self.assertFalse(third["browser"])
                    self.assertEqual(tools.call_count, 1)
                finally:
                    server.shutdown()
                    thread.join(2)

    def test_ordinary_chat_and_negated_actions_do_not_load_tools(self):
        for text in ("Hi", "Explain this project", "What does main.py do?", "Don't edit files",
                     "Show an example Java file", "Write a code snippet", "How to run tests?",
                     "Active editor: main.py\nHello", "Can you explain tool calling?"):
            with self.subTest(text=text):
                self.assertFalse(chat_needs_tools(text))

    def test_explicit_workspace_actions_activate_tools(self):
        for text in ("Read main.py", "Please run project tests", "Can you fix this file?",
                     "Active editor: Main.java\ncompile it", "Use my skills to review this",
                     "Use MCP", "/run python main.py", "List the project files"):
            with self.subTest(text=text):
                self.assertTrue(chat_needs_tools(text))

    def test_context_includes_only_short_text_history_and_explicit_reference(self):
        history = [{"role": "user", "text": "x" * 3000, "logs": ["SECRET LOG"]}] * 9
        messages = chat_messages("hello", history, "EXPLICIT ATTACHMENT")
        self.assertEqual(len(messages), 8)
        self.assertEqual(len(messages[1]["content"]), 1800)
        self.assertNotIn("SECRET LOG", str(messages))
        self.assertIn("EXPLICIT ATTACHMENT", messages[-1]["content"])

    def test_light_followup_skips_old_files_web_and_agent_and_keeps_attached_context(self):
        with TemporaryDirectory() as folder:
            attached = Path(folder) / "attached.txt"
            attached.write_text("EXPLICIT REFERENCE", encoding="utf-8")
            state = JobState()
            state.data["conversation"] = [{"role": "assistant", "text": "", "state": "working"}]
            prior = {"request": "https://example.com", "files": ["old-output.py"], "answer": "old",
                     "generation": "OLD GENERATED OUTPUT", "path": folder}
            with patch("ui_server.create_engine") as factory, patch("ui_server.ensure_ollama"), \
                    patch("ui_server.fetch_public_url") as web, patch("ui_server.validate_path") as old_file, \
                    patch("ui_server.run_agent") as agent:
                factory.return_value.supports_tools.return_value = False
                factory.return_value.generate.return_value = "Hello"
                state.run_chat("hello https://example.com", Settings(), prior, [], [str(attached)], light_chat=True)
                self.assertEqual(state.snapshot()["status"], "succeeded")
                messages = factory.return_value.generate.call_args.args[0]
                self.assertIn("EXPLICIT REFERENCE", str(messages))
                self.assertNotIn("OLD GENERATED OUTPUT", str(messages))
                web.assert_not_called()
                old_file.assert_not_called()
                agent.assert_not_called()

    def test_snapshot_cannot_mutate_message_attachments(self):
        state = JobState()
        state.data["conversation"] = [{"role": "user", "text": "Hi", "attachments": ["a.txt"]}]
        state.snapshot()["conversation"][0]["attachments"].clear()
        self.assertEqual(state.data["conversation"][0]["attachments"], ["a.txt"])
