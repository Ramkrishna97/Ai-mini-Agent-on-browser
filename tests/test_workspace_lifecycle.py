"""Integration checks for scaffolding, build, server, browser, and repair."""

import asyncio
import json
from pathlib import Path
import shutil
import socket
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace

from agent_tools import ToolRuntime
from agent import run_agent
from config import Settings


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LifecycleTests(unittest.TestCase):
    def test_agent_repairs_build_and_browser_failures(self):
        if not shutil.which("npm"):
            self.skipTest("Node/npm is unavailable")
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Python Playwright is unavailable")
        with TemporaryDirectory() as folder:
            root = Path(folder)
            port = free_port()
            broken_server = f"const http = require('node:http');\nhttp.createServer((req,res) => res.end(require('node:fs').readFileSync('index.html'))).listen({port})\nBROKEN SYNTAX\n"
            repaired_server = broken_server.replace("BROKEN SYNTAX\n", "")
            broken_html = "<h1>App</h1><script>throw Error('UI broken')</script>\n"
            fixed_html = "<h1>App</h1><script>document.body.dataset.ready='yes'</script>\n"
            sequence = [
                ("create_project_structure", {"basePath": ".", "fileTree": {
                    "package.json": json.dumps({"scripts": {"build": "node --check server.js"}}),
                    "server.js": broken_server, "index.html": broken_html}}),
                ("run_command", {"command": "npm run build"}),
                None,
                ("apply_file_patch", {"filePath": "server.js", "targetDiff":
                    f"@@ -2,2 +2 @@\n {broken_server.splitlines()[1]}\n-BROKEN SYNTAX\n"}),
                ("run_command", {"command": "npm run build"}),
                ("start_dev_server", {"command": "node server.js", "port": port, "timeoutMs": 5000}),
                ("browser_navigate", {"url": f"http://127.0.0.1:{port}"}),
                ("browser_capture_snapshot", {}),
                ("browser_get_console_logs", {}),
                None,
                ("apply_file_patch", {"filePath": "index.html", "targetDiff":
                    "@@ -1 +1 @@\n-" + broken_html + "+" + fixed_html}),
                ("run_command", {"command": "npm run build"}),
                ("browser_navigate", {"url": f"http://127.0.0.1:{port}"}),
                ("browser_capture_snapshot", {}),
                ("browser_get_console_logs", {}),
            ]

            class ScriptedClient:
                def __init__(self):
                    self.index = 0
                    self.messages = []

                def chat(self, **kwargs):
                    self.messages.append(kwargs["messages"][:])
                    if self.index >= len(sequence):
                        return SimpleNamespace(message=SimpleNamespace(content="Build and browser checks passed.", tool_calls=[]))
                    item = sequence[self.index]
                    self.index += 1
                    if item is None:
                        return SimpleNamespace(message=SimpleNamespace(content="Looks done.", tool_calls=[]))
                    name, arguments = item
                    call = SimpleNamespace(id=f"call_{self.index}", function=SimpleNamespace(name=name, arguments=arguments))
                    return SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))

            client = ScriptedClient()
            answer = asyncio.run(run_agent(root, "Create a working web app", Settings(),
                SimpleNamespace(client=client), lambda *_: True, lambda: None, browser=True))
            self.assertIn("passed", answer)
            self.assertEqual(client.index, len(sequence))
            self.assertTrue(any("Before finishing:" in str(message) for turn in client.messages for message in turn))
            self.assertIn("ready='yes'", (root / "index.html").read_text())

    def test_scaffold_patch_build_and_server(self):
        if not shutil.which("npm"):
            self.skipTest("Node/npm is unavailable")
        with TemporaryDirectory() as folder:
            root = Path(folder)
            port = free_port()

            async def check():
                async with ToolRuntime(root) as runtime:
                    names = {tool["function"]["name"] for tool in runtime.tools}
                    self.assertTrue({"create_project_structure", "apply_file_patch", "start_dev_server",
                                     "wait_for_port", "stop_dev_server"} <= names)
                    project = json.loads(await runtime.call("create_project_structure", {
                        "basePath": "app", "fileTree": {
                            "package.json": json.dumps({"scripts": {"build": "node --check server.js",
                                                               "test": "node --test test.js"}}),
                            "server.js": "const http = require('node:http');\nhttp.createServer((req,res) => res.end('broken')).listen(process.env.PORT);\n",
                            "test.js": "const { test } = require('node:test');\ntest('works', () => {});\n",
                            "src/index.html": "<h1>broken</h1>\n"}}))
                    self.assertEqual(len(project["files"]), 4)
                    with self.assertRaises(ValueError):
                        await runtime.call("create_project_structure", {"basePath": "app", "fileTree": {"../outside": "x"}})
                    for command in ("npm run build", "npm test"):
                        result = json.loads(await runtime.call("run_command", {"command": command, "cwd": "app"}))
                        self.assertEqual(result["exit_code"], 0, result["output"])
                    patch = "@@ -1,2 +1,2 @@\n const http = require('node:http');\n-http.createServer((req,res) => res.end('broken')).listen(process.env.PORT);\n+http.createServer((req,res) => res.end('fixed')).listen(process.env.PORT);\n"
                    await runtime.call("apply_file_patch", {"filePath": "app/server.js", "targetDiff": patch})
                    self.assertIn("fixed", (root / "app/server.js").read_text())
                    with self.assertRaisesRegex(ValueError, "context"):
                        await runtime.call("apply_file_patch", {"filePath": "app/server.js", "targetDiff": patch})
                    started = json.loads(await runtime.call("start_dev_server", {
                        "command": "node server.js", "port": port, "timeoutMs": 5000}))
                    self.assertTrue(started["ready"])
                    self.assertTrue(json.loads(await runtime.call("wait_for_port", {"port": port, "timeoutMs": 1000}))["ready"])
                    stopped = json.loads(await runtime.call("stop_dev_server", {"processId": started["process_id"]}))
                    self.assertTrue(stopped["stopped"])

            # The server command runs in the selected workspace; place a launcher there.
            (root / "server.js").write_text(
                f"process.env.PORT = '{port}'; require('./app/server.js');\n", encoding="utf-8")
            asyncio.run(check())

    def test_browser_console_detects_and_clears_error_after_patch(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Python Playwright is unavailable")
        with TemporaryDirectory() as folder:
            root = Path(folder)
            port = free_port()
            (root / "index.html").write_text("<h1>App</h1><script>throw Error('broken')</script>\n", encoding="utf-8")

            async def check():
                async with ToolRuntime(root, browser=True) as runtime:
                    server = json.loads(await runtime.call("start_dev_server", {
                        "command": f"python -m http.server {port} --bind 127.0.0.1", "port": port,
                        "timeoutMs": 5000}))
                    try:
                        await runtime.call("browser_navigate", {"url": server["url"]})
                        logs = json.loads(await runtime.call("browser_get_console_logs", {}))
                        self.assertTrue(any("broken" in str(event) for event in logs["events"]))
                        await runtime.call("apply_file_patch", {"filePath": "index.html", "targetDiff":
                            "@@ -1 +1 @@\n-<h1>App</h1><script>throw Error('broken')</script>\n+<h1>App</h1><script>document.body.dataset.ready='yes'</script>\n"})
                        await runtime.call("browser_navigate", {"url": server["url"]})
                        snapshot = json.loads(await runtime.call("browser_capture_snapshot", {}))
                        self.assertIn("App", snapshot["text"])
                        self.assertTrue(snapshot["screenshot_base64"])
                        self.assertEqual(json.loads(await runtime.call("browser_get_console_logs", {}))["events"], [])
                    finally:
                        await runtime.call("stop_dev_server", {"processId": server["process_id"]})

            try:
                asyncio.run(check())
            except RuntimeError as exc:
                if "playwright install chromium" in str(exc):
                    self.skipTest("Chromium is unavailable")
                raise


if __name__ == "__main__":
    unittest.main()
