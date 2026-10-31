"""Opt-in, individually approved tools for the local Ollama agent."""

import asyncio
import base64
from contextlib import AsyncExitStack
import ipaddress
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import uuid
from urllib.parse import urlparse

from jsonschema import Draft202012Validator

from command_runner import run_command
from file_manager import validate_location, validate_path
from error_details import describe_error
from web_access import fetch_public_url
from workspace_lifecycle import DevServerManager, apply_file_patch, create_project_structure, wait_for_port
from agent_skills import list_skills, load_skill

LOG = logging.getLogger("workflow")
MAX_RESULT = 16000
MCP_CONFIG_PATH = Path(__file__).resolve().parent / "mcp_servers.json"
LOCAL_MCP_SERVER = Path(__file__).resolve().parent / "local_mcp_server.py"
CORE_MANIFEST_PATH = Path(__file__).resolve().parent / "core_tool_manifest.json"
CORE_MANIFEST = json.loads(CORE_MANIFEST_PATH.read_text(encoding="utf-8"))
CORE_SCHEMAS = {item["name"]: item["input_schema"] for item in CORE_MANIFEST["tools"]}
if len(CORE_MANIFEST["tools"]) != 4 or set(CORE_SCHEMAS) != {"read_file", "write_file", "run_command", "list_directory"}:
    raise ValueError("Core tool manifest must declare the four supported core tools exactly once.")
for input_schema in CORE_SCHEMAS.values():
    Draft202012Validator.check_schema(input_schema)
CORE_VALIDATORS = {name: Draft202012Validator(input_schema)
                   for name, input_schema in CORE_SCHEMAS.items()}
CORE_TOOLS = [{"type": "function", "function": {"name": item["name"],
               "description": item["description"], "parameters": item["input_schema"]}}
              for item in CORE_MANIFEST["tools"]]


def validate_local_mcp_url(value):
    """Accept only explicit loopback Streamable HTTP endpoints."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("MCP URL must be a local HTTP URL.")
    parsed = urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname or not parsed.path or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("MCP URL must be an HTTP loopback endpoint with a path and no credentials.")
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MCP URL must use a valid loopback address and port.") from exc
    if not loopback or port is None:
        raise ValueError("MCP URL must use a loopback address and explicit port.")
    return value


def schema(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}}


BUILTINS = [
    *CORE_TOOLS,
    schema("list_files", "List files in a local directory (one level).", {"path": {"type": "string"}}, ["path"]),
    schema("search_text", "Search text files under the workspace for a literal string; returns at most 50 matching lines.",
           {"query": {"type": "string"}, "path": {"type": "string"}}, ["query"]),
    schema("list_skills", "List available workspace agent skills and their descriptions.", {}),
    schema("load_skill", "Load one workspace skill's instructions on demand. Skill text is guidance, never permission to bypass tool approval.",
           {"name": {"type": "string"}}, ["name"]),
    schema("replace_text", "Replace an exact text fragment in a UTF-8 file. Fails if the fragment is missing or occurs more than once; creates a backup.",
           {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
           ["path", "old_text", "new_text"]),
    schema("create_project_structure", "Create up to 100 new UTF-8 project files in one call; paths are relative to basePath inside the workspace.",
           {"basePath": {"type": "string"}, "fileTree": {"type": "object", "additionalProperties": {"type": "string"}}},
           ["basePath", "fileTree"]),
    schema("apply_file_patch", "Apply context-checked unified diff hunks to a UTF-8 file; creates a backup.",
           {"filePath": {"type": "string"}, "targetDiff": {"type": "string"}}, ["filePath", "targetDiff"]),
    schema("start_dev_server", "Start a background development server in the workspace and wait briefly for its localhost port.",
           {"command": {"type": "string"}, "port": {"type": "integer"}, "timeoutMs": {"type": "integer"}}, ["command", "port"]),
    schema("wait_for_port", "Poll a localhost TCP port until it accepts connections or the timeout expires.",
           {"port": {"type": "integer"}, "timeoutMs": {"type": "integer"}}, ["port", "timeoutMs"]),
    schema("stop_dev_server", "Stop a background server started in this agent session.",
           {"processId": {"type": "string"}}, ["processId"]),
    schema("make_directory", "Create a local directory and missing parents.",
           {"path": {"type": "string"}}, ["path"]),
    schema("move_path", "Move or rename a file or directory. The destination must not exist.",
           {"source": {"type": "string"}, "destination": {"type": "string"}}, ["source", "destination"]),
    schema("delete_path", "Delete a file or directory. Files are moved to a .bak backup; directories and their contents are removed recursively.",
           {"path": {"type": "string"}}, ["path"]),
    schema("web_fetch", "Read a public website or GitHub repository URL. Returns bounded page text or repository metadata and README. No local/private URLs.",
           {"url": {"type": "string"}}, ["url"]),
    schema("browser", "Control a headless Chromium browser. Actions: navigate(url), snapshot(), click(selector), type(selector,text).",
           {"action": {"type": "string", "enum": ["navigate", "snapshot", "click", "type"]},
            "url": {"type": "string"}, "selector": {"type": "string"}, "text": {"type": "string"}}, ["action"]),
    schema("browser_navigate", "Navigate headless Chromium to a localhost URL.", {"url": {"type": "string"}}, ["url"]),
    schema("browser_capture_snapshot", "Capture a base64 PNG screenshot and bounded DOM state.", {}),
    schema("browser_get_console_logs", "Return JavaScript errors, unhandled rejections, and HTTP 404/500 failures since navigation.", {}),
    schema("browser_interact", "Click or type into a page element using a CSS selector.",
           {"action": {"type": "string", "enum": ["click", "type"]}, "selector": {"type": "string"},
            "value": {"type": "string"}}, ["action", "selector"]),
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
        self.browser_events = []
        self.servers = DevServerManager(workspace)
        self.tools = [tool for tool in BUILTINS if browser or not tool["function"]["name"].startswith("browser")]

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
        from mcp.client.streamable_http import streamable_http_client
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
            if "url" in item:
                if set(item) != {"url"}:
                    raise ValueError(f"HTTP MCP server {server_name} accepts only a url.")
                transport = await self.stack.enter_async_context(streamable_http_client(validate_local_mcp_url(item["url"])))
                client = await self.stack.enter_async_context(ClientSession(*transport[:2]))
            else:
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
            await asyncio.to_thread(self.servers.close)
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

    def workspace_path(self, value):
        path = self.local_path(value)
        if not path.is_relative_to(self.workspace.resolve()) or path == self.workspace.resolve():
            raise ValueError("This operation requires a path inside the selected workspace.")
        return path

    async def call(self, name, arguments):
        if self.cancel_check:
            self.cancel_check()
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        if name in CORE_VALIDATORS:
            error = next(CORE_VALIDATORS[name].iter_errors(arguments), None)
            if error:
                raise ValueError(f"Invalid {name} arguments: {error.message}")
        if name == "list_skills":
            return list_skills(self.workspace)
        if name == "load_skill":
            return load_skill(self.workspace, arguments.get("name"))
        if name in {"list_files", "list_directory"}:
            path = self.local_path(arguments.get("path") or ".")
            if not path.is_dir():
                raise ValueError("Not a directory.")
            return json.dumps([{"name": x.name, "type": "directory" if x.is_dir() else "file"}
                               for x in sorted(path.iterdir())[:200]])
        if name == "read_file":
            path = validate_path(str(self.local_path(arguments.get("path"))))
            if not path.is_file() or path.stat().st_size > 65536:
                raise ValueError("File missing or larger than 64 KB.")
            return path.read_text(encoding="utf-8-sig")
        if name == "search_text":
            query = arguments.get("query")
            if not isinstance(query, str) or not 0 < len(query) <= 200:
                raise ValueError("Query must contain 1-200 characters.")
            base = self.workspace_path(arguments.get("path")) if arguments.get("path") else self.workspace
            if not base.exists():
                raise ValueError("Search path does not exist.")
            matches = []
            skip = {".git", ".venv", "venv", "node_modules", "dist", "build", ".gradle"}
            paths = [base] if base.is_file() else None
            for folder, directories, files in ([] if paths else os.walk(base, followlinks=False)):
                directories[:] = [item for item in directories if item not in skip and
                                  not (Path(folder) / item).is_symlink()]
                paths = (paths or []) + [Path(folder) / item for item in files]
                if len(paths) > 2000:
                    break
            for path in paths or []:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 256_000:
                    continue
                try:
                    with path.open(encoding="utf-8-sig") as handle:
                        for number, line in enumerate(handle, 1):
                            if query.casefold() in line.casefold():
                                matches.append({"path": str(path), "line": number, "text": line.strip()[:300]})
                                if len(matches) >= 50:
                                    return json.dumps(matches)
                except (UnicodeError, OSError):
                    continue
            return json.dumps(matches)
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
        if name == "replace_text":
            from file_manager import assert_unchanged, atomic_write, create_backup, read_snapshot
            path = validate_path(str(self.workspace_path(arguments.get("path"))))
            old, new = arguments.get("old_text"), arguments.get("new_text")
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise ValueError("old_text must be nonempty and new_text must be text.")
            old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
            snapshot = read_snapshot(path, 1_000_000, allow_empty=True)
            if snapshot.text.count(old) != 1:
                raise ValueError("old_text must occur exactly once in the file.")
            updated = snapshot.text.replace(old, new, 1)
            if len(updated.encode("utf-8")) > 1_000_000:
                raise ValueError("Updated file exceeds 1 MB.")
            backup = create_backup(snapshot)
            atomic_write(path, snapshot.encode(updated), snapshot.mode,
                         lambda: assert_unchanged(snapshot))
            return f"Updated {path}; backup {backup}"
        if name == "create_project_structure":
            return json.dumps(await asyncio.to_thread(create_project_structure, self.workspace,
                arguments.get("basePath"), arguments.get("fileTree")))
        if name == "apply_file_patch":
            return json.dumps(await asyncio.to_thread(apply_file_patch, self.workspace,
                arguments.get("filePath"), arguments.get("targetDiff")))
        if name == "start_dev_server":
            return json.dumps(await asyncio.to_thread(self.servers.start, arguments.get("command"),
                arguments.get("port"), arguments.get("timeoutMs", 2_000)))
        if name == "wait_for_port":
            return json.dumps(await asyncio.to_thread(wait_for_port, arguments.get("port"),
                arguments.get("timeoutMs"), self.cancel_check))
        if name == "stop_dev_server":
            return json.dumps(await asyncio.to_thread(self.servers.stop, arguments.get("processId")))
        if name == "make_directory":
            path = self.workspace_path(arguments.get("path"))
            if path.exists():
                raise ValueError("Directory already exists.")
            path.mkdir(parents=True)
            return f"Created {path}"
        if name == "move_path":
            source = self.workspace_path(arguments.get("source"))
            destination = self.workspace_path(arguments.get("destination"))
            if not source.exists() or destination.exists():
                raise ValueError("Source must exist and destination must not exist.")
            if source.is_dir() and destination.is_relative_to(source):
                raise ValueError("Cannot move a directory into itself.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            return f"Moved {source} to {destination}"
        if name == "delete_path":
            path = self.workspace_path(arguments.get("path"))
            if not path.exists():
                raise ValueError("Path does not exist.")
            if path.is_dir():
                shutil.rmtree(path)
                return f"Deleted directory {path}"
            path = validate_path(str(path))
            backup = path.with_name(f"{path.name}.{uuid.uuid4().hex}.bak")
            shutil.copy2(path, backup)
            path.unlink()
            return f"Deleted {path}; backup {backup}"
        if name == "run_command":
            command = arguments.get("command")
            if not isinstance(command, str) or not 0 < len(command) <= 2000:
                raise ValueError("Command must contain 1-2000 characters.")
            cwd = self.local_path(arguments["cwd"]) if "cwd" in arguments else self.workspace
            if not cwd.is_dir():
                raise ValueError("Command cwd must be an existing directory.")
            return json.dumps(await asyncio.to_thread(run_command, command, cwd,
                                                     cancel_check=self.cancel_check))
        if name == "web_fetch":
            return json.dumps(await asyncio.to_thread(fetch_public_url, arguments.get("url")))
        if name == "browser" and self.browser_enabled:
            return await self.browser_call(arguments)
        if name.startswith("browser_") and self.browser_enabled:
            action = name.removeprefix("browser_")
            if action == "interact":
                action = arguments.get("action")
            return await self.browser_call({**arguments, "action": action})
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
                page = self.browser_page
                page.on("console", lambda message: self.browser_events.append({"type": "console", "text": message.text})
                        if message.type == "error" else None)
                page.on("pageerror", lambda error: self.browser_events.append({"type": "pageerror", "text": str(error)}))
                page.on("response", lambda response: self.browser_events.append(
                    {"type": "http", "status": response.status, "url": response.url})
                    if response.status >= 400 else None)
                page.on("requestfailed", lambda request: self.browser_events.append(
                    {"type": "network", "url": request.url, "failure": request.failure}))
                await page.add_init_script("""window.addEventListener('unhandledrejection', event => {
                    console.error('Unhandled rejection: ' + String(event.reason));
                });""")
            except Exception as exc:
                raise RuntimeError("Browser unavailable. Run: python -m playwright install chromium") from exc
        action = arguments.get("action")
        page = self.browser_page
        if action == "navigate":
            url = arguments.get("url", "")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Browser URL must use localhost.")
            self.browser_events.clear()
            await page.goto(url, wait_until="load", timeout=30000)
            await page.wait_for_timeout(50)
        elif action == "click":
            await page.locator(arguments["selector"]).first.click(timeout=10000)
        elif action == "type":
            await page.locator(arguments["selector"]).first.fill(arguments.get("value", arguments.get("text", "")), timeout=10000)
        elif action == "get_console_logs":
            return json.dumps({"events": self.browser_events[-100:]})
        elif action not in {"snapshot", "capture_snapshot"}:
            raise ValueError("Unknown browser action.")
        result = {"url": page.url, "title": await page.title(),
                           "text": (await page.locator("body").inner_text(timeout=10000))[:MAX_RESULT],
                           "links": await page.locator("a[href]").evaluate_all(
                               "nodes => nodes.slice(0,50).map(n => ({text:n.innerText.slice(0,100), href:n.href}))")}
        if action == "capture_snapshot":
            result["screenshot_base64"] = base64.b64encode(await page.screenshot(full_page=True)).decode("ascii")
            result["elements"] = await page.locator("input, button, a, select, textarea").evaluate_all(
                "nodes => nodes.slice(0,100).map(n => ({tag:n.tagName.toLowerCase(), text:(n.innerText||'').slice(0,100), id:n.id, name:n.name}))")
        return json.dumps(result)


class _BrowserContext:
    def __init__(self, browser):
        self.browser = browser

    async def __aenter__(self):
        return self.browser

    async def __aexit__(self, *_):
        await self.browser.close()
