"""Run an official llama.cpp server against a selected local GGUF, without Ollama."""

import atexit
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

from config import GenerationError, ModelError
from file_manager import validate_location

LOG = logging.getLogger("workflow")
ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime" / "llama"
GITHUB_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=5"


def validate_gguf(value: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise ValueError("Choose a local .gguf model file.")
    path = validate_location(value)
    if path.suffix.casefold() != ".gguf" or not path.is_file():
        raise ValueError("Choose an existing .gguf model file.")
    with path.open("rb") as handle:
        if handle.read(4) != b"GGUF":
            raise ValueError("The selected file is not a GGUF model.")
    return path


def _find_server(folder: Path) -> Path | None:
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    matches = list(folder.rglob(name)) if folder.is_dir() else []
    return matches[0] if matches else None


def ensure_llama_server() -> Path:
    """Find an installed runtime, or cache the verified official Windows CPU build."""
    configured = os.environ.get("LLAMA_SERVER_PATH")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise ModelError(f"LLAMA_SERVER_PATH does not exist: {path}")
        return path
    cached = _find_server(RUNTIME)
    if cached:
        return cached
    found = shutil.which("llama-server") or shutil.which("llama-server.exe")
    if found:
        return Path(found).resolve()
    if os.name != "nt":
        raise ModelError("Install llama.cpp's llama-server or set LLAMA_SERVER_PATH; Ollama is not required.")
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64" if machine in {"amd64", "x86_64"} else ""
    if not architecture:
        raise ModelError("Automatic llama.cpp setup supports Windows x64/arm64. Set LLAMA_SERVER_PATH for another build.")
    LOG.info("Downloading official llama.cpp Windows CPU runtime (once)...")
    try:
        request = Request(GITHUB_RELEASES, headers={"User-Agent": "ai-agent-mini", "Accept": "application/vnd.github+json"})
        with urlopen(request, timeout=20) as response:
            releases = json.load(response)
        asset = next((asset for release in releases for asset in release.get("assets", [])
                      if asset.get("name", "").endswith(f"-bin-win-cpu-{architecture}.zip")
                      and str(asset.get("digest", "")).startswith("sha256:")), None)
        if asset is None:
            raise ModelError("No verified Windows CPU build was found in the recent official llama.cpp releases.")
        RUNTIME.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="llama-download-", dir=RUNTIME.parent) as temporary:
            temporary = Path(temporary)
            archive = temporary / "runtime.zip"
            digest = hashlib.sha256()
            size = 0
            with urlopen(Request(asset["browser_download_url"], headers={"User-Agent": "ai-agent-mini"}), timeout=120) as response:
                with archive.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > 500_000_000:
                            raise ModelError("llama.cpp download exceeded 500 MB.")
                        digest.update(chunk)
                        output.write(chunk)
            if digest.hexdigest().lower() != asset["digest"].split(":", 1)[1].lower():
                raise ModelError("llama.cpp download checksum did not match its official release digest.")
            extracted = temporary / "extracted"
            extracted.mkdir()
            with zipfile.ZipFile(archive) as package:
                members = package.infolist()
                if len(members) > 250 or sum(item.file_size for item in members) > 1_000_000_000:
                    raise ModelError("llama.cpp archive exceeds its extraction limit.")
                for item in members:
                    destination = (extracted / item.filename).resolve()
                    if not destination.is_relative_to(extracted) or item.filename.startswith(("/", "\\")):
                        raise ModelError("llama.cpp archive contains an unsafe path.")
                    if item.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with package.open(item) as source, destination.open("wb") as output:
                            shutil.copyfileobj(source, output)
            binary = _find_server(extracted)
            if binary is None:
                raise ModelError("Official llama.cpp archive did not contain llama-server.exe.")
            relative = binary.relative_to(extracted)
            if RUNTIME.exists():
                cached = _find_server(RUNTIME)
                if cached:
                    return cached
                raise ModelError("The llama.cpp runtime cache is incomplete; inspect .runtime/llama before retrying.")
            extracted.rename(RUNTIME)
            return RUNTIME / relative
    except ModelError:
        raise
    except (OSError, ValueError, KeyError, URLError, zipfile.BadZipFile) as exc:
        raise ModelError(f"Could not install llama.cpp: {exc}. Install llama-server manually or set LLAMA_SERVER_PATH.") from exc


class GgufEngine:
    def __init__(self, settings, *, progress=None):
        self.settings = settings
        self.progress = progress
        self.target = ""
        self.process = None
        self.base_url = ""
        self.client = self  # adapter for the existing approved agent loop
        atexit.register(self.close)

    def notify(self, phase, **values):
        if self.progress:
            self.progress({"phase": phase, "model": Path(self.settings.model_name).name,
                           "target": self.target, **values})

    def set_target(self, target):
        self.target = target

    def health_check(self):
        if self.process and self.process.poll() is None:
            return
        model = validate_gguf(self.settings.model_name)
        self.notify("loading", text="", started_at=time.time())
        binary = ensure_llama_server()
        # Bind only to loopback. Choosing an unused port is a short-lived race;
        # startup checks detect a conflicting listener rather than using it.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        argv = [str(binary), "--model", str(model), "--alias", model.name, "--jinja",
                "--ctx-size", str(self.settings.context_tokens), "--host", "127.0.0.1", "--port", str(port)]
        LOG.info("Loading local GGUF model: %s", model)
        self.process = subprocess.Popen(argv, cwd=binary.parent, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + max(120, self.settings.timeout)
        while time.monotonic() < deadline:
            self.notify("loading")
            if self.process.poll() is not None:
                self.close()
                raise ModelError("llama-server exited while loading this GGUF. Check model compatibility and available memory.")
            try:
                with urlopen(self.base_url + "/health", timeout=2) as response:
                    if response.status == 200 and json.load(response).get("status") == "ok":
                        self.notify("ready")
                        return
            except (OSError, URLError, ValueError):
                pass
            time.sleep(0.5)
        self.close()
        raise ModelError("Timed out loading the GGUF model. Try a smaller model or increase the timeout.")

    def close(self):
        process = self.process
        self.process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def _request(self, messages, *, tools=None, stream=False, json_output=False, max_tokens=None):
        if not self.process or self.process.poll() is not None:
            raise ModelError("GGUF runtime is not running. Load the model first.")
        normalized = []
        for message in messages:
            if isinstance(message, dict):
                item = dict(message)
            else:
                item = dict(message.raw)
            if item.get("role") == "tool":
                item.pop("tool_name", None)
            normalized.append(item)
        body = {"model": Path(self.settings.model_name).name, "messages": normalized,
                "temperature": self.settings.temperature, "max_tokens": max_tokens or self.settings.max_tokens,
                "stream": stream}
        if tools:
            body["tools"] = tools
        if json_output:
            body["response_format"] = {"type": "json_object"}
        request = Request(self.base_url + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            return urlopen(request, timeout=self.settings.timeout)
        except HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            raise ModelError(f"GGUF request failed ({exc.code}): {detail}") from exc
        except (OSError, URLError) as exc:
            raise ModelError(f"GGUF runtime is unavailable: {exc}") from exc

    def generate(self, messages, *, json_output=False):
        self.notify("generating", text="", started_at=time.time(), generated_tokens=0)
        chunks = []
        count = 0
        finished = None
        done = False
        try:
            with self._request(messages, stream=True, json_output=json_output) as response:
                for line in response:
                    self.notify("generating")
                    if not line.startswith(b"data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == b"[DONE]":
                        done = True
                        break
                    event = json.loads(raw)
                    choice = event["choices"][0]
                    finished = choice.get("finish_reason") or finished
                    delta = choice.get("delta", {}).get("content") or ""
                    if delta:
                        count += len(delta)
                        if count > self.settings.max_tokens * 32:
                            raise GenerationError("GGUF response exceeded the output size limit.")
                        chunks.append(delta)
                        self.notify("generating", delta=delta)
            if not done or not finished:
                raise GenerationError("GGUF response stream ended before completion.")
            if finished == "length":
                raise GenerationError("GGUF model hit max_tokens; increase output tokens or simplify the request.")
            content = "".join(chunks)
            if not content.strip():
                raise GenerationError("GGUF model returned an empty response.")
            self.notify("generated")
            return content
        except (ModelError, GenerationError):
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise ModelError(f"GGUF generation failed: {exc}") from exc

    def chat(self, *, messages, tools=None, **_):
        """Ollama-shaped result for the agent; wire messages remain OpenAI-compatible."""
        try:
            with self._request(messages, tools=tools) as response:
                choice = json.load(response)["choices"][0]
            message = choice["message"]
            calls = [SimpleNamespace(id=item["id"], function=SimpleNamespace(
                name=item["function"]["name"], arguments=item["function"].get("arguments", "{}")))
                     for item in message.get("tool_calls", [])]
            return SimpleNamespace(message=SimpleNamespace(content=message.get("content") or "",
                                                         tool_calls=calls, raw=message))
        except ModelError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise ModelError(f"GGUF agent response failed: {exc}") from exc

    def chat_agent(self, messages, tools):
        """Stream OpenAI-compatible text and assemble indexed tool-call fragments."""
        self.notify("generating", text="", activity="", started_at=time.time(), generated_tokens=0)
        parts, fragments = [], {}
        size, finish, done, tool_streaming = 0, None, False, False
        try:
            with self._request(messages, tools=tools, stream=True) as response:
                for line in response:
                    self.notify("tool" if tool_streaming else "generating")
                    if not line.startswith(b"data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == b"[DONE]":
                        done = True
                        break
                    choice = json.loads(raw)["choices"][0]
                    finish = choice.get("finish_reason") or finish
                    delta = choice.get("delta") or {}
                    content = delta.get("content") or ""
                    if content:
                        size += len(content)
                        if size > self.settings.max_tokens * 32:
                            raise GenerationError("Agent response exceeded the output size limit.")
                        parts.append(content)
                        self.notify("tool" if tool_streaming else "generating", delta=content)
                    for item in delta.get("tool_calls") or []:
                        if not tool_streaming:
                            tool_streaming = True
                            self.notify("tool", activity="Preparing tool request")
                        slot = fragments.setdefault(item.get("index", 0), {"id": "", "type": "function",
                            "function": {"name": "", "arguments": ""}})
                        slot["id"] += item.get("id") or ""
                        function = item.get("function") or {}
                        slot["function"]["name"] += function.get("name") or ""
                        slot["function"]["arguments"] += function.get("arguments") or ""
            if not done or not finish:
                raise GenerationError("GGUF agent stream ended before completion.")
            if finish == "length":
                raise GenerationError("GGUF agent response hit the output token limit.")
            content = "".join(parts)
            raw_calls = [fragments[index] for index in sorted(fragments)]
            calls = [SimpleNamespace(id=item["id"], function=SimpleNamespace(
                name=item["function"]["name"], arguments=item["function"]["arguments"]))
                     for item in raw_calls]
            raw_message = {"role": "assistant", "content": content, "tool_calls": raw_calls}
            if raw_calls:
                self.notify("tool", activity="Tool request ready")
            else:
                self.notify("generated")
            return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls,
                                                         raw=raw_message))
        except (ModelError, GenerationError):
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise ModelError(f"GGUF agent streaming failed: {exc}") from exc
