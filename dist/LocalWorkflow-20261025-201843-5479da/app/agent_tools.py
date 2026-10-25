"""Opt-in, individually approved tools for the local Ollama agent."""

import asyncio
from contextlib import AsyncExitStack
import json
import logging
from pathlib import Path
import sys
from urllib.parse import urlparse

from command_runner import run_command
from file_manager import validate_location, validate_path
from error_details import describe_error
from web_access import fetch_public_url

LOG = logging.getLogger("workflow")
MAX_RESULT = 16000
MCP_CONFIG_PATH = Path(__file__).resolve().parent / "mcp_servers.json"
LOCAL_MCP_SERVER = Path(__file__).resolve().parent / "local_mcp_server.py"


def schema(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}}


BUILTINS = [
    schema("list_files", "List files in a local directory (one level).", {"path": {"type": "string"}}, ["path"]),
    schema("read_file", "Read a UTF-8 local text file, at most 64 KB.", {"path": {"type": "string"}}, ["path"]),
    schema("write_file", "Write a UTF-8 local text file; existing files get a .bak backup.",
           {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    schema("run_command", "Execute a PowerShell command on Windows (shell elsewhere) in the selected workspace; 120-second limit.",
           {"command": {"type": "string"}}, ["command"]),
    schema("web_fetch", "Read a public website or GitHub repository URL. Returns bounded page text or repository metadata and README. No local/private URLs.",
           {"url": {"type": "string"}}, ["url"]),
    schema("browser", "Control a headless Chromium browser. Actions: navigate(url), snapshot(), click(selector), type(selector,text).",
           {"action": {"type": "string", "enum": ["navigate", "snapshot", "click", "type"]},
            "url": {"type": "string"}, "selector": {"type": "string"}, "text": {"type": "string"}}, ["action"]),
]


class ToolRuntime:
    def __init__(self, workspace: Path, *, browser=False, mcp=False, cancel_check=None):
        self.workspace = workspace
        self.browser_enabled = browser
        self.mcp_enabled = mcp
        self.cancel_check = cancel_check
        self.stack = AsyncExitStack()
        self.mcp_clients = {}
        self.browser_driver = None
        self.browser_page = None
        self.tools = [tool for tool in BUILTINS if browser or tool["function"]["name"] != "browser"]

    async def __aenter__(self):
        await self.stack.__aenter__()
        try:
            if self.mcp_enabled:
                try:
                    await self.connect_mcp()
                except Exception as exc:
                    LOG.warning("MCP unavailable; continuing with built-in tools: %s", describe_error(exc))
                    self.mcp_enabled = False
                    self.mcp_clients.clear()
                    self.tools = [tool for tool in self.tools if not tool["function"]["name"].startswith("mcp_")]
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def connect_mcp(self):
        from mcp import ClientSession, StdioServerParameters, types
        from mcp.client.stdio import stdio_client
        servers = {"local": {"command": sys.executable, "args": [str(LOCAL_MCP_SERVER)]}}
        if MCP_CONFIG_PATH.is_file():
            config = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
            extra = config.get("mcpServers")
            if not isinstance(extra, dict) or len(extra) > 5 or "local" in extra:
                raise ValueError("mcp_servers.json needs an mcpServers object with at most 5 extra servers; 'local' is reserved.")
            servers.update(extra)
        for server_name, item in servers.items():
            if not isinstance(server_name, str) or not server_name.isidentifier() or not isinstance(item, dict):
                raise ValueError("Invalid MCP server name or configuration.")
            command, args, env = item.get("command"), item.get("args", []), item.get("env")
            if (not isinstance(command, str) or not command or not isinstance(args, list)
                    or not all(isinstance(x, str) for x in args) or
                    (env is not None and (not isinstance(env, dict) or
                                          not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())))):
                raise ValueError(f"Invalid command/args/env for MCP server {server_name}.")
            transport = await self.stack.enter_async_context(stdio_client(StdioServerParameters(
                command=command, args=args, cwd=str(self.workspace), env=env)))
            client = await self.stack.enter_async_context(ClientSession(*transport))
            await client.initialize()
            self.mcp_clients[server_name] = client
            cursor = None
            while True:
                listed = await client.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
                for tool in listed.tools:
                    name = f"mcp_{server_name}_{tool.name}"
                    if len(name) > 64 or any(t["function"]["name"] == name for t in self.tools):
                        LOG.warning("Skipping duplicate or long MCP tool name: %s", name)
                        continue
                    self.tools.append({"type": "function", "function": {
                        "name": name, "description": (tool.description or tool.name)[:500],
                        "parameters": getattr(tool, "input_schema", None)
                        or getattr(tool, "inputSchema", None)}})
                cursor = getattr(listed, "next_cursor", None) or getattr(listed, "nextCursor", None)
                if not cursor:
                    break
            LOG.info("MCP server connected: %s", server_name)

    async def __aexit__(self, *args):
        try:
            try:
                await self.stack.__aexit__(*args)
            except Exception as exc:
                LOG.warning("Optional tool cleanup failed: %s", describe_error(exc))
        finally:
            if self.browser_driver:
                try:
                    await self.browser_driver.stop()
                except Exception as exc:
                    LOG.warning("Browser cleanup failed: %s", describe_error(exc))

    def local_path(self, value):
        if not isinstance(value, str) or not value or len(value) > 1000:
            raise ValueError("A file path is required (maximum 1000 characters).")
        path = Path(value).expanduser()
        return validate_location(str(path if path.is_absolute() else self.workspace / path))

    async def call(self, name, arguments):
        if self.cancel_check:
            self.cancel_check()
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        if name == "list_files":
            path = self.local_path(arguments.get("path"))
            if not path.is_dir():
                raise ValueError("Not a directory.")
            return json.dumps([{"name": x.name, "type": "directory" if x.is_dir() else "file"}
                               for x in sorted(path.iterdir())[:200]])
        if name == "read_file":
            path = validate_path(str(self.local_path(arguments.get("path"))))
            if not path.is_file() or path.stat().st_size > 65536:
                raise ValueError("File missing or larger than 64 KB.")
            return path.read_text(encoding="utf-8-sig")
        if name == "write_file":
            from file_manager import (assert_missing, assert_unchanged, atomic_write,
                                      create_backup, create_parent_dirs, read_snapshot)
            path = self.local_path(arguments.get("path"))
            content = arguments.get("content")
            if not isinstance(content, str) or len(content.encode("utf-8")) > 65536:
                raise ValueError("Content must be UTF-8 text of at most 64 KB.")
            validate_path(str(path), must_exist=False, allow_missing_parents=True)
            create_parent_dirs(path)
            snapshot = read_snapshot(path, 65536, allow_empty=True) if path.exists() else None
            backup = create_backup(snapshot) if snapshot else None
            atomic_write(path, snapshot.encode(content) if snapshot else content.encode("utf-8"),
                         snapshot.mode if snapshot else 0o600,
                         (lambda: assert_unchanged(snapshot)) if snapshot else (lambda: assert_missing(path)),
                         overwrite=bool(snapshot))
            return f"Saved {path}" + (f"; backup {backup}" if backup else "")
        if name == "run_command":
            command = arguments.get("command")
            if not isinstance(command, str) or not 0 < len(command) <= 2000:
                raise ValueError("Command must contain 1-2000 characters.")
            return json.dumps(await asyncio.to_thread(run_command, command, self.workspace,
                                                     cancel_check=self.cancel_check))
        if name == "web_fetch":
            return json.dumps(await asyncio.to_thread(fetch_public_url, arguments.get("url")))
        if name == "browser" and self.browser_enabled:
            return await self.browser_call(arguments)
        if name.startswith("mcp_") and self.mcp_enabled:
            for server_name, client in self.mcp_clients.items():
                prefix = f"mcp_{server_name}_"
                if name.startswith(prefix):
                    result = await client.call_tool(name[len(prefix):], arguments)
                    return json.dumps({"is_error": getattr(result, "is_error", None)
                                       if hasattr(result, "is_error") else getattr(result, "isError", False),
                                       "content": [part.model_dump(exclude_none=True) for part in result.content]}, default=str)
        raise ValueError(f"Unknown or disabled tool: {name}")

    async def browser_call(self, arguments):
        if self.browser_page is None:
            try:
                from playwright.async_api import async_playwright
                self.browser_driver = await async_playwright().start()
                browser = await self.stack.enter_async_context(_BrowserContext(await self.browser_driver.chromium.launch(headless=True)))
                self.browser_page = await browser.new_page()
            except Exception as exc:
                raise RuntimeError("Browser unavailable. Run: python -m playwright install chromium") from exc
        action = arguments.get("action")
        page = self.browser_page
        if action == "navigate":
            url = arguments.get("url", "")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Browser URL must be http(s).")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        elif action == "click":
            await page.locator(arguments["selector"]).first.click(timeout=10000)
        elif action == "type":
            await page.locator(arguments["selector"]).first.fill(arguments["text"], timeout=10000)
        elif action != "snapshot":
            raise ValueError("Unknown browser action.")
        return json.dumps({"url": page.url, "title": await page.title(),
                           "text": (await page.locator("body").inner_text(timeout=10000))[:MAX_RESULT],
                           "links": await page.locator("a[href]").evaluate_all(
                               "nodes => nodes.slice(0,50).map(n => ({text:n.innerText.slice(0,100), href:n.href}))")})


class _BrowserContext:
    def __init__(self, browser):
        self.browser = browser

    async def __aenter__(self):
        return self.browser

    async def __aexit__(self, *_):
        await self.browser.close()
