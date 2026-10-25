"""Public website reading and nested async errors stay bounded and visible."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_tools import ToolRuntime
from error_details import describe_error
from web_access import fetch_public_url, public_url, urls_in_text


class WebAccessTests(unittest.TestCase):
    def test_local_and_private_addresses_are_rejected(self):
        for url in ("http://localhost/", "http://127.0.0.1/", "http://169.254.169.254/",
                    "file:///secret", "https://example.com:8080/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url(url)

    def test_github_repository_fetch_includes_readme(self):
        with patch("web_access.public_url", side_effect=lambda url: url), patch(
            "web_access._fetch_text", side_effect=[
                {"url": "https://api.github.com/repos/example/demo", "text":
                 '{"full_name":"example/demo","description":"Demo","language":"Java"}'},
                {"url": "https://api.github.com/repos/example/demo/readme", "text": "Build with Maven"}]):
            result = fetch_public_url("https://github.com/example/demo")
        self.assertEqual(result["repository"]["language"], "Java")
        self.assertIn("Maven", result["repository"]["readme"])

    def test_approved_web_tool_is_available_without_chromium(self):
        with TemporaryDirectory() as folder:
            async def check():
                async with ToolRuntime(Path(folder)) as runtime:
                    self.assertIn("web_fetch", [item["function"]["name"] for item in runtime.tools])
                    with patch("agent_tools.fetch_public_url", return_value={"url": "https://example.com", "text": "hello"}):
                        return await runtime.call("web_fetch", {"url": "https://example.com"})
            self.assertIn("hello", asyncio.run(check()))

    def test_taskgroup_detail_is_unwrapped(self):
        error = ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("MCP process exited")])
        self.assertEqual(describe_error(error), "MCP process exited")
        self.assertEqual(urls_in_text("See https://github.com/example/demo."), ["https://github.com/example/demo"])


if __name__ == "__main__":
    unittest.main()
