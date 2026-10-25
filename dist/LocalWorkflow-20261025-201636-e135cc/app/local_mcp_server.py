"""Local MCP tools for the Ollama agent.

The server is started over stdio by the application.  Tools are deliberately
bounded and the host application still asks for approval before each call.
"""

import asyncio
import os
from pathlib import Path
import re
import sys

from mcp.server.mcpserver import MCPServer

from command_runner import run_command
from web_access import fetch_public_url

WORKSPACE = Path.cwd().resolve()
IGNORED = {".git", ".venv", ".runtime", "node_modules", "__pycache__"}
server = MCPServer("local-workspace")
MAX_OUTPUT = 16_000
LANGUAGE_COMMANDS = {
    "python": lambda source: [sys.executable, "-c", source],
    "javascript": lambda source: ["node", "-e", source],
    "java": None,
    "npm": None,
}


@server.tool()
def search_text(query: str, path: str = ".") -> list[dict]:
    """Search for literal text in UTF-8 workspace files; return up to 50 matching lines.

    Args:
        query: Case-insensitive literal text to find (1-200 characters).
        path: Workspace-relative file or folder to search, default current workspace.
    """
    if not isinstance(query, str) or not 1 <= len(query) <= 200:
        raise ValueError("Search text must contain 1-200 characters.")
    if not isinstance(path, str) or not path or len(path) > 500:
        raise ValueError("Search path must contain 1-500 characters.")
    selected = (WORKSPACE / path).resolve()
    if not selected.is_relative_to(WORKSPACE):
        raise ValueError("Search path must stay within the selected workspace.")
    if not selected.exists():
        raise ValueError("Search path does not exist.")
    if selected.is_symlink() or selected.is_junction():
        raise ValueError("Linked paths are not searched.")

    def files():
        if selected.is_file():
            yield selected
            return
        visited = 0
        for folder, directories, filenames in os.walk(selected, followlinks=False):
            directories[:] = sorted(name for name in directories
                                    if name.casefold() not in IGNORED
                                    and not (Path(folder) / name).is_symlink()
                                    and not (Path(folder) / name).is_junction())
            for name in sorted(filenames):
                candidate = Path(folder) / name
                if candidate.is_symlink() or candidate.is_junction() or not candidate.is_file():
                    continue
                yield candidate
                visited += 1
                if visited >= 200:
                    return

    matches = []
    needle = query.casefold()
    for candidate in files():
        try:
            if candidate.stat().st_size > 65536:
                continue
            content = candidate.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeError):
            continue
        if "\x00" in content:
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if needle in line.casefold():
                matches.append({"path": candidate.relative_to(WORKSPACE).as_posix(),
                                "line": number, "text": line[:500]})
                if len(matches) >= 50:
                    return matches
    return matches


@server.tool()
def fetch_url(url: str) -> dict:
    """Fetch bounded text from a public HTTP(S) URL."""
    return fetch_public_url(url)


@server.tool()
async def run_code(language: str, code: str, filename: str = "main") -> dict:
    """Run a short Python, JavaScript, Java, or npm task in the workspace.

    Python and JavaScript are passed directly to their local runtimes. Java is
    compiled and run from a temporary workspace file. npm accepts a package
    script name such as ``test`` and runs ``npm run <script>``.
    """
    if not isinstance(language, str) or language.casefold() not in LANGUAGE_COMMANDS:
        raise ValueError("language must be python, javascript, java, or npm")
    if not isinstance(code, str) or not 1 <= len(code) <= 20_000:
        raise ValueError("code must contain 1-20,000 characters")
    language = language.casefold()
    if language == "npm":
        script = code.strip()
        if not re.fullmatch(r"[A-Za-z0-9:_-]+", script):
            raise ValueError("npm script must be a simple package script name")
        command = f"npm run {script}"
    elif language == "java":
        if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", filename):
            raise ValueError("Java filename must be a valid class name")
        source = code.replace("public class Main", f"public class {filename}", 1)
        java_file = WORKSPACE / f"{filename}.java"
        java_file.write_text(source, encoding="utf-8")
        command = f'javac "{java_file.name}"; if ($LASTEXITCODE -eq 0) {{ java "{filename}" }}'
    else:
        extension = "py" if language == "python" else "js"
        source_file = WORKSPACE / f".ollama_mcp_{filename}.{extension}"
        source_file.write_text(code, encoding="utf-8")
        command = (f'& "{sys.executable}" "{source_file.name}"' if language == "python"
                   else f'node "{source_file.name}"')
    result = await asyncio.to_thread(run_command, command, WORKSPACE)
    result["output"] = result.get("output", "")[-MAX_OUTPUT:]
    return result


_browser = None
_page = None


@server.tool()
async def browser(action: str, url: str = "", selector: str = "", text: str = "") -> dict:
    """Navigate and inspect a headless Chromium browser, or click/type on it."""
    global _browser, _page
    if action not in {"navigate", "snapshot", "click", "type"}:
        raise ValueError("action must be navigate, snapshot, click, or type")
    if _page is None:
        from playwright.async_api import async_playwright
        _browser = await async_playwright().start()
        chromium = await _browser.chromium.launch(headless=True)
        _page = await chromium.new_page()
    if action == "navigate":
        if not url.startswith(("http://", "https://")):
            raise ValueError("browser URL must be http(s)")
        await _page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    elif action == "click":
        await _page.locator(selector).first.click(timeout=10_000)
    elif action == "type":
        await _page.locator(selector).first.fill(text, timeout=10_000)
    return {"url": _page.url, "title": await _page.title(),
            "text": (await _page.locator("body").inner_text(timeout=10_000))[:MAX_OUTPUT]}


if __name__ == "__main__":
    server.run(transport="stdio")
