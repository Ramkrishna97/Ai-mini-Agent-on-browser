"""Validated model settings and engines for Ollama or local GGUF inference."""

from dataclasses import dataclass
import logging
import time
from types import SimpleNamespace
from urllib.parse import urlparse


class ModelError(RuntimeError):
    """The selected local model runtime cannot satisfy a generation request."""


class GenerationError(ModelError):
    """An incomplete/empty model result that the program may retry."""


class WorkflowCancelled(ModelError):
    """The user cancelled the fixed workflow."""


LOG = logging.getLogger("workflow")


@dataclass(frozen=True)
class Settings:
    endpoint: str = "http://localhost:11434"
    model_name: str = "qwen2.5-coder:7b"
    temperature: float = 0.2
    max_tokens: int = 4096
    context_tokens: int = 16384
    timeout: float = 180.0
    chunk_chars: int = 6000
    max_file_bytes: int = 1_000_000
    max_chunks: int = 200
    provider: str = "ollama"
    keep_alive: str | int = "10m"

    def __post_init__(self) -> None:
        if self.provider not in {"ollama", "gguf"}:
            raise ValueError("Model provider must be ollama or gguf.")
        if self.provider == "ollama":
            url = urlparse(self.endpoint)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("Endpoint must be an http(s) URL.")
        if not self.model_name.strip():
            raise ValueError("Model name cannot be empty.")
        if not 0 <= self.temperature <= 2:
            raise ValueError("Temperature must be between 0 and 2.")
        if not 0 < self.timeout <= 3600:
            raise ValueError("Timeout must be between 0 and 3600 seconds.")
        for name in ("max_tokens", "context_tokens", "chunk_chars", "max_file_bytes", "max_chunks"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive.")
        # Conservative character budget, including bounded instruction and prompts.
        if self.chunk_chars + self.max_tokens + 6000 > self.context_tokens:
            raise ValueError("Context budget too small: increase input context or reduce output tokens.")


class OllamaEngine:
    def __init__(self, settings: Settings, client=None, *, progress=None) -> None:
        self.settings = settings
        self.progress = progress
        self.target = ""
        if client is None:
            try:
                from ollama import Client
            except ImportError as exc:
                raise ModelError("Install dependencies with: python -m pip install -r requirements.txt") from exc
            client = Client(host=settings.endpoint, timeout=settings.timeout)
        self.client = client

    def notify(self, phase: str, **values) -> None:
        if self.progress:
            self.progress({"phase": phase, "model": self.settings.model_name,
                           "target": self.target, **values})

    def set_target(self, target: str) -> None:
        self.target = target

    def list_models(self) -> list[dict]:
        """Discover installed models without loading a fixed or default model."""
        try:
            result = []
            for item in self.client.list().models:
                if not item.model:
                    continue
                # Embedding-only models cannot generate files. Keep them visible but
                # mark them unavailable for this particular operation.
                entry = {"name": item.model, "size": item.size or 0,
                         "can_generate": False, "can_use_tools": False, "reason": ""}
                if item.model.endswith((":cloud", "-cloud")):
                    entry["reason"] = "cloud model; local models only"
                else:
                    try:
                        details = self.client.show(item.model)
                        capabilities = details.capabilities or []
                        entry["can_generate"] = not capabilities or "completion" in capabilities
                        entry["can_use_tools"] = "tools" in capabilities
                        if not entry["can_generate"]:
                            entry["reason"] = "embedding only"
                    except Exception:
                        # One retired or broken installation must not hide working models.
                        entry["reason"] = "unavailable in Ollama"
                result.append(entry)
            return sorted(result, key=lambda item: item["name"].casefold())
        except Exception as exc:
            raise ModelError(f"Cannot list installed models. Check that Ollama is running: {exc}") from exc

    def health_check(self) -> None:
        """Check service/model availability, load the model, then confirm residency."""
        model = self.settings.model_name
        canonical = model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"
        try:
            LOG.info("Checking Ollama and loading model: %s", model)
            self.notify("loading", text="", started_at=time.time())
            installed = {item.model for item in self.client.list().models}
            if canonical not in installed:
                raise ModelError(f"Model {canonical!r} is not installed. Run: ollama pull {model}")
            # An empty prompt loads the model; it does not request a workflow decision.
            self.client.generate(model=model, prompt="", keep_alive=self.settings.keep_alive)
            loaded = {item.model for item in self.client.ps().models}
            if canonical not in loaded:
                raise ModelError(f"Ollama did not load {canonical!r}.")
            LOG.info("Confirmed model loaded in Ollama: %s", canonical)
            self.notify("ready")
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError(f"Ollama health check failed at {self.settings.endpoint}: {exc}") from exc

    def generate(self, messages: list[dict[str, str]], *, json_output: bool = False) -> str:
        """One independent turn; stream to the UI when a progress callback is supplied."""
        try:
            streaming = self.progress is not None
            self.notify("generating", text="", started_at=time.time(), generated_tokens=0)
            response = self.client.chat(
                model=self.settings.model_name,
                messages=messages,
                stream=streaming,
                think=False,
                **({"format": "json"} if json_output else {}),
                keep_alive=self.settings.keep_alive,
                options={
                    "temperature": self.settings.temperature,
                    "num_predict": self.settings.max_tokens,
                    "num_ctx": self.settings.context_tokens,
                },
            )
            if streaming:
                parts, size, final = [], 0, None
                stream = response
                deadline = time.monotonic() + self.settings.timeout
                try:
                    for chunk in stream:
                        self.notify("generating")
                        if time.monotonic() > deadline:
                            raise ModelError("Generation exceeded its time limit. Reduce the task or use a different model.")
                        final = chunk
                        content = chunk.message.content or ""
                        if content:
                            size += len(content)
                            if size > self.settings.max_tokens * 32:
                                raise GenerationError("Streamed response exceeds the output size limit.")
                            parts.append(content)
                            self.notify("generating", delta=content)
                    if final is None or not final.done:
                        raise GenerationError("Ollama stream ended without a completion message.")
                    content = "".join(parts)
                    response = final
                finally:
                    close = getattr(stream, "close", None)
                    if close:
                        close()
            else:
                content = response.message.content
            if response.done_reason == "length":
                raise GenerationError("Model hit max_tokens; increase the output token budget or simplify the task.")
            if not isinstance(content, str) or not content.strip():
                raise GenerationError("Model returned an empty response.")
            count = getattr(response, "eval_count", None)
            duration = (getattr(response, "eval_duration", 0) or 0) / 1e9
            LOG.info("Ollama response from %s: %s tokens in %.2fs", self.settings.model_name, count or "unknown", duration)
            self.notify("generated", generated_tokens=count, duration=duration)
            return content
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError(f"Ollama generation failed: {exc}") from exc

    def chat_agent(self, messages, tools):
        """Stream visible answer text while retaining tool calls for the next turn."""
        self.notify("generating", text="", activity="", started_at=time.time(), generated_tokens=0)
        parts, calls, size, final = [], [], 0, None
        try:
            stream = self.client.chat(model=self.settings.model_name, messages=messages, tools=tools,
                stream=True, think=False, keep_alive=self.settings.keep_alive, options={
                    "temperature": self.settings.temperature, "num_predict": self.settings.max_tokens,
                    "num_ctx": self.settings.context_tokens})
            try:
                for chunk in stream:
                    self.notify("tool" if calls else "generating")
                    final = chunk
                    content = chunk.message.content or ""
                    if content:
                        size += len(content)
                        if size > self.settings.max_tokens * 32:
                            raise GenerationError("Agent response exceeded the output size limit.")
                        parts.append(content)
                        self.notify("tool" if calls else "generating", delta=content)
                    incoming_calls = getattr(chunk.message, "tool_calls", None) or []
                    if incoming_calls and not calls:
                        self.notify("tool", activity="Preparing tool request")
                    calls.extend(incoming_calls)
                if final is None or not final.done:
                    raise GenerationError("Agent response stream ended before completion.")
                if getattr(final, "done_reason", None) == "length":
                    raise GenerationError("Agent response hit the output token limit.")
            finally:
                close = getattr(stream, "close", None)
                if close:
                    close()
            content = "".join(parts)
            self.notify("tool" if calls else "generated", generated_tokens=getattr(final, "eval_count", None),
                        activity="Tool request ready" if calls else "")
            raw = {"role": "assistant", "content": content,
                   "tool_calls": [call.model_dump(exclude_none=True) for call in calls]}
            return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls, raw=raw))
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError(f"Ollama agent streaming failed: {exc}") from exc


def create_engine(settings: Settings, *, progress=None):
    """Select an Ollama or direct local-GGUF runtime for the same workflow."""
    if settings.provider == "gguf":
        from gguf_engine import GgufEngine
        return GgufEngine(settings, progress=progress)
    return OllamaEngine(settings, progress=progress)
