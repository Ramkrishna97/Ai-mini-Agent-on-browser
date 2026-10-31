"""Offline checks for approval-gated agent calls and local file boundaries."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent import json_tool_request, json_tool_requests, mcp_result_data, run_agent, wrapped_tool_request
from agent_tools import CORE_MANIFEST, ToolRuntime, validate_local_mcp_url
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
    def test_local_http_mcp_url_rejects_non_loopback(self):
        for url in ("https://127.0.0.1:3001/mcp", "http://example.com:3001/mcp",
                    "http://127.0.0.1/mcp", "http://user:pass@127.0.0.1:3001/mcp"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_local_mcp_url(url)
        self.assertEqual(validate_local_mcp_url("http://127.0.0.1:3001/mcp"),
                         "http://127.0.0.1:3001/mcp")

    def test_local_http_mcp_server_connects_and_calls_tool(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "example.txt").write_text("local mcp marker", encoding="utf-8")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            process = subprocess.Popen([sys.executable, str(agent_tools.LOCAL_MCP_SERVER),
                                        "--http", "--port", str(port)], cwd=root,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            original = agent_tools.MCP_CONFIG_PATH
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        self.fail("Local HTTP MCP server exited during startup")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.1):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail("Local HTTP MCP server did not open its port")
                config = root / "mcp_servers.json"
                config.write_text(json.dumps({"mcpServers": {"fixture": {
                    "url": f"http://127.0.0.1:{port}/mcp"}}}), encoding="utf-8")
                agent_tools.MCP_CONFIG_PATH = config

                async def check():
                    async with ToolRuntime(root, mcp=True) as runtime:
                        names = [x["function"]["name"] for x in runtime.tools]
                        self.assertIn("mcp_fixture_search_text", names)
                        self.assertNotIn("mcp_fixture_run_code", names)
                        result = json.loads(await runtime.call("mcp_fixture_search_text", {"query": "local mcp marker"}))
                        self.assertIn("example.txt", json.dumps(result))

                asyncio.run(check())
            finally:
                agent_tools.MCP_CONFIG_PATH = original
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def test_mcp_result_metadata_is_language_agnostic(self):
        payload = {"content": [{"type": "text", "text": "not JSON"},
                               {"type": "text", "text": json.dumps({"files": ["report.txt"],
                                                                         "exit_code": 2})}]}
        self.assertEqual(mcp_result_data(json.dumps(payload)), {"files": ["report.txt"], "exit_code": 2})

    def test_malformed_tool_json_is_not_reported_as_completed_work(self):
        class BrokenClient:
            def chat(self, **kwargs):
                return SimpleNamespace(message=SimpleNamespace(content=(
                    '{"name":"mcp_local_run_code","arguments":{"language":"java"}}\n'
                    '{"state":"done"}'), tool_calls=[]))

        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ModelError, "invalid or incomplete tool request"):
                asyncio.run(run_agent(Path(folder), "Write and run Java code", Settings(),
                    SimpleNamespace(client=BrokenClient()), lambda *_: True, lambda: None))

    def test_plain_answer_is_not_forced_into_language_specific_tools(self):
        class EmptyClient:
            def chat(self, **kwargs):
                return SimpleNamespace(message=SimpleNamespace(
                    content="A class can encapsulate state and behavior.", tool_calls=[]))

        with TemporaryDirectory() as folder:
            answer = asyncio.run(run_agent(Path(folder), "Explain how to write a class", Settings(),
                SimpleNamespace(client=EmptyClient()), lambda *_: True, lambda: None))
            self.assertEqual(answer, "A class can encapsulate state and behavior.")

    def test_repeated_failing_command_trips_circuit_breaker(self):
        class RepeatingClient:
            def chat(self, **kwargs):
                call = SimpleNamespace(function=SimpleNamespace(
                    name="run_command", arguments={"command": "pytest"}), id=None)
                return SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))

        with TemporaryDirectory() as folder:
            approvals = []
            failed = json.dumps({"exit_code": 1, "diagnostics": {
                "status": "failed", "summary": "test failed", "errors": []}})
            with patch.object(ToolRuntime, "call", new_callable=AsyncMock, return_value=failed) as execute:
                answer = asyncio.run(run_agent(Path(folder), "Fix tests", Settings(),
                    SimpleNamespace(client=RepeatingClient()),
                    lambda name, args: approvals.append(name) or True, lambda: None))
            self.assertIn("Stopped after repeated attempts", answer)
            self.assertEqual(approvals, ["run_command"] * 3)
            self.assertEqual(execute.await_count, 3)

    def test_successful_edit_allows_command_retry(self):
        class FixingClient:
            def __init__(self):
                self.calls = 0

            def chat(self, **kwargs):
                self.calls += 1
                if self.calls == 6:
                    return SimpleNamespace(message=SimpleNamespace(content="Fixed.", tool_calls=[]))
                name = "write_file" if self.calls == 4 else "run_command"
                arguments = ({"path": "fix.txt", "content": "fixed"} if name == "write_file"
                             else {"command": "pytest"})
                call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments), id=None)
                return SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))

        async def execute(_runtime, name, _arguments):
            if name == "write_file":
                return "Saved fix.txt"
            return json.dumps({"exit_code": 1, "diagnostics": {"summary": "test failed"}})

        with TemporaryDirectory() as folder:
            approvals = []
            with patch.object(ToolRuntime, "call", execute):
                answer = asyncio.run(run_agent(Path(folder), "Fix tests", Settings(),
                    SimpleNamespace(client=FixingClient()),
                    lambda name, args: approvals.append(name) or True, lambda: None))
            self.assertIn("Fixed.", answer)
            self.assertIn("fix.txt", answer)
            self.assertEqual(approvals, ["run_command"] * 3 + ["write_file", "run_command"])

    def test_core_manifest_schemas_are_advertised_and_enforced(self):
        names = {item["name"] for item in CORE_MANIFEST["tools"]}
        self.assertEqual(names, {"read_file", "write_file", "run_command", "list_directory"})
        with TemporaryDirectory() as folder:
            root = Path(folder)

            async def check():
                async with ToolRuntime(root) as runtime:
                    advertised = {item["function"]["name"]: item["function"]["parameters"]
                                  for item in runtime.tools}
                    for item in CORE_MANIFEST["tools"]:
                        self.assertEqual(advertised[item["name"]], item["input_schema"])
                        self.assertFalse(advertised[item["name"]]["additionalProperties"])
                    with self.assertRaisesRegex(ValueError, "Invalid write_file arguments"):
                        await runtime.call("write_file", {"path": "data.txt"})
                    with self.assertRaisesRegex(ValueError, "Invalid read_file arguments"):
                        await runtime.call("read_file", {"path": 123})
                    with self.assertRaisesRegex(ValueError, "Invalid list_directory arguments"):
                        await runtime.call("list_directory", {"path": ".", "unexpected": True})
                    (root / "data.txt").write_text("hello", encoding="utf-8")
                    listed = json.loads(await runtime.call("list_directory", {"path": "."}))
                    self.assertEqual(listed, [{"name": "data.txt", "type": "file"}])

            asyncio.run(check())

    def test_run_command_honors_explicit_cwd(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            nested = root / "nested"
            nested.mkdir()

            async def check():
                async with ToolRuntime(root) as runtime:
                    with patch("agent_tools.run_command", return_value={"exit_code": 0, "output": "ok"}) as execute:
                        await runtime.call("run_command", {"command": "pwd", "cwd": "nested"})
                        self.assertEqual(execute.call_args.args[1], nested.resolve())
                    with self.assertRaisesRegex(ValueError, "existing directory"):
                        await runtime.call("run_command", {"command": "pwd", "cwd": "missing"})

            asyncio.run(check())

    def test_exact_json_tool_request_uses_approval(self):
        class JsonClient:
            def __init__(self):
                self.messages = []

            def chat(self, **kwargs):
                self.messages.append(kwargs["messages"][:])
                content = '{"name":"read_file","arguments":{"path":"data.txt"}}' if len(self.messages) == 1 else "Done."
                return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))

        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data.txt").write_text("secret", encoding="utf-8")
            client = JsonClient()
            approvals = []
            answer = asyncio.run(run_agent(root, "Read it", Settings(), SimpleNamespace(client=client),
                                           lambda name, args: approvals.append((name, args)) or True, lambda: None))
            self.assertEqual(answer, "Done.")
            self.assertEqual(approvals[0][0], "read_file")
            self.assertEqual(client.messages[1][-1]["content"], "secret")

    def test_json_tool_request_requires_exact_advertised_tool(self):
        tools = [{"function": {"name": "read_file"}}]
        self.assertIsNone(json_tool_request('{"name":"run_command","arguments":{}}', tools))
        self.assertIsNone(json_tool_request('{"name":"read_file","arguments":{},"extra":1}', tools))
        self.assertIsNotNone(json_tool_request('```json\n{"name":"read_file","arguments":{}}\n```', tools))
        self.assertIsNotNone(json_tool_request('```json\n{"name":"read_file","arguments":{}}\n```\nI read it.', tools))
        self.assertEqual(len(json_tool_requests('{"name":"read_file","arguments":{}}\n'
                                                '{"name":"read_file","arguments":{}}', tools)), 2)

    def test_complete_tagged_tool_call_is_approved(self):
        tools = [{"function": {"name": "read_file"}}]
        self.assertEqual(wrapped_tool_request('<tool_call>\n<function=read_file>\n'
            '{"path":"data.txt"}\n</function>\n</tool_call>', tools),
            {"name": "read_file", "arguments": {"path": "data.txt"}})
        self.assertIsNone(wrapped_tool_request('<tool_call>\n<function=read_file>', tools))

    def test_incomplete_tagged_tool_call_retries_then_fails_clearly(self):
        class BrokenClient:
            def __init__(self):
                self.messages = []

            def chat(self, **kwargs):
                self.messages.append(kwargs["messages"][:])
                return SimpleNamespace(message=SimpleNamespace(
                    content="<tool_call>\n<function=run_command>", tool_calls=[]))

        with TemporaryDirectory() as folder:
            client = BrokenClient()
            approvals = []
            with self.assertRaisesRegex(ModelError, "incomplete tool request"):
                asyncio.run(run_agent(Path(folder), "Run it", Settings(), SimpleNamespace(client=client),
                                      lambda name, args: approvals.append(name) or True, lambda: None))
            self.assertEqual(len(client.messages), 3)
            self.assertEqual(approvals, [])

    def test_incomplete_tagged_tool_call_can_recover(self):
        class RecoveringClient:
            def __init__(self):
                self.turns = 0

            def chat(self, **kwargs):
                self.turns += 1
                content = ("<tool_call>\n<function=read_file>" if self.turns == 1 else
                           '<tool_call><function=read_file>{"path":"data.txt"}</function></tool_call>' if self.turns == 2 else
                           "Read the file.")
                return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))

        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data.txt").write_text("hello", encoding="utf-8")
            approvals = []
            answer = asyncio.run(run_agent(root, "Read data", Settings(),
                SimpleNamespace(client=RecoveringClient()),
                lambda name, args: approvals.append(name) or True, lambda: None))
            self.assertEqual(answer, "Read the file.")
            self.assertEqual(approvals, ["read_file"])

    def test_invalid_command_cwd_trips_retry_breaker(self):
        class RepeatingClient:
            def chat(self, **kwargs):
                call = SimpleNamespace(function=SimpleNamespace(
                    name="run_command", arguments={"command": "pytest", "cwd": "missing"}), id=None)
                return SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))

        with TemporaryDirectory() as folder:
            approvals = []
            with patch.object(ToolRuntime, "call", new_callable=AsyncMock,
                              side_effect=ValueError("Command cwd must be an existing directory.")) as execute:
                answer = asyncio.run(run_agent(Path(folder), "Run tests", Settings(),
                    SimpleNamespace(client=RepeatingClient()),
                    lambda name, args: approvals.append(name) or True, lambda: None))
            self.assertIn("Stopped after repeated attempts", answer)
            self.assertEqual(approvals, ["run_command"] * 3)
            self.assertEqual(execute.await_count, 3)

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

    def test_workspace_edit_move_and_delete_tools(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "notes.txt").write_text("hello world\n", encoding="utf-8")

            async def check():
                async with ToolRuntime(root) as runtime:
                    self.assertIn("notes.txt", await runtime.call("search_text", {"query": "hello"}))
                    await runtime.call("replace_text", {"path": "notes.txt", "old_text": "hello", "new_text": "goodbye"})
                    await runtime.call("make_directory", {"path": "nested"})
                    await runtime.call("move_path", {"source": "notes.txt", "destination": "nested/notes.txt"})
                    await runtime.call("delete_path", {"path": "nested/notes.txt"})

            asyncio.run(check())
            self.assertFalse((root / "nested" / "notes.txt").exists())
            self.assertEqual(len(list((root / "nested").glob("notes.txt.*.bak"))), 1)

    def test_destructive_tools_refuse_workspace_root_and_outside_paths(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)

            async def check():
                async with ToolRuntime(root) as runtime:
                    with self.assertRaisesRegex(ValueError, "inside the selected workspace"):
                        await runtime.call("delete_path", {"path": "."})
                    with self.assertRaisesRegex(ValueError, "inside the selected workspace"):
                        await runtime.call("make_directory", {"path": "../outside"})

            asyncio.run(check())

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
