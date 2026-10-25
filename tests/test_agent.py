"""Offline checks for approval-gated agent calls and local file boundaries."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import run_agent
from agent_tools import ToolRuntime
import agent_tools
from config import Settings
from config import ModelError
from ui_server import JobState


class FakeClient:
    def __init__(self):
        self.messages = []

    def chat(self, **kwargs):
        self.messages.append(kwargs["messages"][:])
        if len(self.messages) == 1:
            call = SimpleNamespace(function=SimpleNamespace(name="read_file", arguments={"path": "data.txt"}))
            return SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))
        return SimpleNamespace(message=SimpleNamespace(content="Done.", tool_calls=[]))


class AgentTests(unittest.TestCase):
    def test_plain_tool_code_is_not_treated_as_execution(self):
        class TextOnlyClient:
            def chat(self, **kwargs):
                return SimpleNamespace(message=SimpleNamespace(
                    content='```tool_code\njava -jar "missing.jar"\n```', tool_calls=[]))

        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ModelError, "No command was executed"):
                asyncio.run(run_agent(Path(folder), "Run it", Settings(),
                                      SimpleNamespace(client=TextOnlyClient()),
                                      lambda *_: True, lambda: None))

    def test_mcp_failure_does_not_block_builtin_tools(self):
        with TemporaryDirectory() as folder:
            async def check():
                with patch.object(ToolRuntime, "connect_mcp", side_effect=ExceptionGroup(
                    "unhandled errors in a TaskGroup", [RuntimeError("server failed to start")])):
                    async with ToolRuntime(Path(folder), mcp=True) as runtime:
                        self.assertFalse(runtime.mcp_enabled)
                        self.assertIn("web_fetch", [item["function"]["name"] for item in runtime.tools])
                        return json.loads(await runtime.call("list_files", {"path": "."}))
            self.assertEqual(asyncio.run(check()), [])

    def test_java_command_with_mcp_enabled(self):
        if os.name != "nt" or not shutil.which("java") or not shutil.which("javac"):
            self.skipTest("Java JDK on Windows is required")
        with TemporaryDirectory() as folder:
            (Path(folder) / "Hello.java").write_text(
                'public class Hello { public static void main(String[] args) { System.out.println("Hello"); } }',
                encoding="utf-8")
            async def check():
                async with ToolRuntime(Path(folder), mcp=True) as runtime:
                    return json.loads(await runtime.call("run_command", {"command":
                        "javac Hello.java; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; java Hello"}))
            result = asyncio.run(check())
            self.assertEqual(result["exit_code"], 0, result["output"])
            self.assertIn("Hello", result["output"])

    def test_denied_call_is_not_executed(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data.txt").write_text("secret", encoding="utf-8")
            client = FakeClient()
            answer = asyncio.run(run_agent(root, "Read data", Settings(),
                                           SimpleNamespace(client=client), lambda *_: False, lambda: None))
            self.assertEqual(answer, "Done.")
            self.assertIn("Denied by user", client.messages[1][-1]["content"])
            self.assertNotIn("secret", client.messages[1][-1]["content"])

    def test_reference_context_keeps_gguf_compatible_roles(self):
        with TemporaryDirectory() as folder:
            client = FakeClient()
            answer = asyncio.run(run_agent(Path(folder), "Summarize the repository", Settings(),
                                           SimpleNamespace(client=client), lambda *_: False, lambda: None,
                                           reference_context="Repository README text"))
            self.assertEqual(answer, "Done.")
            self.assertEqual([message["role"] for message in client.messages[0]], ["system", "user"])
            self.assertIn("Repository README text", client.messages[0][1]["content"])
            self.assertIn("Summarize the repository", client.messages[0][1]["content"])

    def test_approved_read_and_write_backup(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data.txt").write_text("before", encoding="utf-8")
            async def check():
                async with ToolRuntime(root) as runtime:
                    self.assertEqual(await runtime.call("read_file", {"path": "data.txt"}), "before")
                    await runtime.call("write_file", {"path": "data.txt", "content": "after"})
            asyncio.run(check())
            self.assertEqual((root / "data.txt").read_text(encoding="utf-8"), "after")
            self.assertEqual(len(list(root.glob("data.txt.*.bak"))), 1)

    def test_approval_matches_pending_request(self):
        state = JobState()
        state.data["status"] = "running"
        result = []
        worker = threading.Thread(target=lambda: result.append(state.await_approval("read_file", {"path": "a"})))
        worker.start()
        for _ in range(50):
            pending = state.snapshot()["pending_tool"]
            if pending:
                break
            time.sleep(0.01)
        self.assertIsNotNone(pending)
        self.assertFalse(state.approve("wrong", True))
        self.assertTrue(state.approve(pending["id"], False))
        worker.join(2)
        self.assertEqual(result, [False])
        self.assertIsNone(state.snapshot()["pending_tool"])

    def test_allow_always_covers_all_tools_in_this_conversation(self):
        state = JobState()
        state.data["status"] = "running"
        results = []
        worker = threading.Thread(target=lambda: results.append(state.await_approval(
            "run_command", {"command": "Write-Output first"})))
        worker.start()
        for _ in range(50):
            pending = state.snapshot()["pending_tool"]
            if pending:
                break
            time.sleep(0.01)
        self.assertIsNotNone(pending)
        self.assertFalse(state.approve("wrong", True, always=True))
        self.assertFalse(state.snapshot()["always_allow_all"])
        self.assertTrue(state.approve(pending["id"], True, always=True))
        worker.join(2)
        self.assertEqual(results, [True])
        self.assertTrue(state.snapshot()["always_allow_all"])
        self.assertTrue(state.await_approval("run_command", {"command": "Write-Output different"}))
        self.assertIsNone(state.snapshot()["pending_tool"])
        for name in ("write_file", "web_fetch", "browser", "mcp_local_search"):
            self.assertTrue(state.await_approval(name, {"path": "x"}))
            self.assertIsNone(state.snapshot()["pending_tool"])

        restored = JobState()
        restored.restore({"data": state.snapshot(), "settings": None})
        self.assertTrue(restored.await_approval("browser", {"action": "navigate"}))
        self.assertFalse(JobState().snapshot()["always_allow_all"])

    def test_allow_always_does_not_override_cancellation(self):
        state = JobState()
        state.data["always_allow_all"] = True
        state.cancel_event.set()
        from config import WorkflowCancelled
        with self.assertRaises(WorkflowCancelled):
            state.await_approval("run_command", {"command": "Write-Output should-not-run"})

    def test_mcp_stdio_tool_is_discovered_and_called(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "server.py").write_text(
                "from mcp.server.mcpserver import MCPServer\n"
                "server = MCPServer('fixture')\n"
                "@server.tool()\n"
                "def echo(text: str) -> str:\n    return text\n"
                "server.run(transport='stdio')\n", encoding="utf-8")
            (root / "mcp_servers.json").write_text(json.dumps({"mcpServers": {
                "fixture": {"command": sys.executable, "args": [str(root / "server.py")]}}}), encoding="utf-8")
            original = agent_tools.MCP_CONFIG_PATH
            agent_tools.MCP_CONFIG_PATH = root / "mcp_servers.json"
            try:
                async def check():
                    async with ToolRuntime(root, mcp=True) as runtime:
                        self.assertIn("mcp_fixture_echo", [x["function"]["name"] for x in runtime.tools])
                        result = json.loads(await runtime.call("mcp_fixture_echo", {"text": "hello"}))
                        self.assertFalse(result["is_error"])
                        self.assertIn("hello", str(result["content"]))
                asyncio.run(check())
            finally:
                agent_tools.MCP_CONFIG_PATH = original

    def test_mcp_works_without_configuration_file(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "notes.txt").write_text("Searchable example line\n", encoding="utf-8")
            original = agent_tools.MCP_CONFIG_PATH
            agent_tools.MCP_CONFIG_PATH = root / "missing.json"
            try:
                async def check():
                    async with ToolRuntime(root, mcp=True) as runtime:
                        self.assertIn("mcp_local_search_text", [x["function"]["name"] for x in runtime.tools])
                        result = json.loads(await runtime.call("mcp_local_search_text", {"query": "searchable"}))
                        self.assertFalse(result["is_error"])
                        self.assertIn("notes.txt", str(result["content"]))
                asyncio.run(check())
            finally:
                agent_tools.MCP_CONFIG_PATH = original

    def test_browser_launches_when_chromium_is_installed(self):
        async def check():
            async with ToolRuntime(Path.cwd(), browser=True) as runtime:
                return json.loads(await runtime.call("browser", {"action": "snapshot"}))
        try:
            result = asyncio.run(check())
        except RuntimeError as exc:
            if "playwright install chromium" in str(exc):
                self.skipTest("Chromium browser binary is not installed")
            raise
        self.assertEqual(result["url"], "about:blank")


if __name__ == "__main__":
    unittest.main()
