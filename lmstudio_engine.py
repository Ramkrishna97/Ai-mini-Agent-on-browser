"""LM Studio's local OpenAI-compatible chat endpoint for the agent loop."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import ModelError, Settings
from gguf_engine import GgufEngine


def base_url(endpoint: str) -> str:
    return endpoint.rstrip("/").removesuffix("/v1")


def list_models(endpoint: str) -> list[dict]:
    """Use the native catalog so unloaded downloaded models remain selectable."""
    base = base_url(endpoint)
    try:
        with urlopen(base + "/api/v1/models", timeout=10) as response:
            models = json.load(response)["models"]
        return [{"name": item["key"], "label": item.get("display_name") or item["key"],
                 "size": item.get("size_bytes", 0), "loaded": bool(item.get("loaded_instances")),
                 "can_generate": item.get("type") == "llm",
                 "can_use_tools": bool(item.get("capabilities", {}).get("trained_for_tool_use"))}
                for item in models if item.get("key")]
    except HTTPError as exc:
        if exc.code != 404:
            raise ModelError(f"LM Studio model list failed ({exc.code}).") from exc
    except (OSError, URLError, ValueError, KeyError) as exc:
        raise ModelError(f"Cannot connect to LM Studio at {base}: {exc}") from exc
    try:
        with urlopen(base + "/v1/models", timeout=10) as response:
            models = json.load(response)["data"]
        return [{"name": item["id"], "label": item["id"], "can_generate": True,
                 "can_use_tools": True} for item in models if item.get("id")]
    except (OSError, URLError, ValueError, KeyError) as exc:
        raise ModelError(f"Cannot list LM Studio models at {base}: {exc}") from exc


def _native_model_request(endpoint: str, action: str, payload: dict) -> dict:
    request = Request(base_url(endpoint) + f"/api/v1/models/{action}",
                      data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=300 if action == "load" else 30) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError("Expected an object from LM Studio.")
        return result
    except HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise ModelError(f"LM Studio {action} failed ({exc.code}): {detail}") from exc
    except (OSError, URLError, ValueError) as exc:
        raise ModelError(f"LM Studio {action} failed: {exc}") from exc


def load_model(endpoint: str, model: str, context_length: int) -> str:
    if not isinstance(model, str) or not 0 < len(model.strip()) <= 200:
        raise ValueError("Choose an LM Studio model.")
    if type(context_length) is not int or not 16384 <= context_length <= 262144:
        raise ValueError("LM Studio agent context must be between 16384 and 262144 tokens.")
    Settings(provider="lmstudio", endpoint=endpoint, model_name=model.strip(), context_tokens=context_length)
    result = _native_model_request(endpoint, "load", {"model": model.strip(), "context_length": context_length})
    instance_id = result.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ModelError("LM Studio did not return a loaded model instance.")
    return instance_id


def unload_model(endpoint: str, instance_id: str) -> None:
    _native_model_request(endpoint, "unload", {"instance_id": instance_id})


class LMStudioEngine(GgufEngine):
    """Reuse the streaming OpenAI chat adapter without starting llama.cpp."""

    def health_check(self):
        if self.settings.model_name not in {item["name"] for item in list_models(self.settings.endpoint)
                                            if item["can_generate"]}:
            raise ModelError(f"Model '{self.settings.model_name}' is not available in LM Studio.")

    def close(self):
        pass  # LM Studio owns its server and model lifetime.

    def _request(self, messages, *, tools=None, stream=False, json_output=False, max_tokens=None):
        normalized = []
        for message in messages:
            item = dict(message) if isinstance(message, dict) else dict(message.raw)
            if item.get("role") == "tool":
                item.pop("tool_name", None)
            normalized.append(item)
        body = {"model": self.settings.model_name, "messages": normalized,
                "temperature": self.settings.temperature,
                "max_tokens": max_tokens or self.settings.max_tokens,
                "stream": stream, "ttl": 600}
        if tools:
            body["tools"] = tools
        if json_output:
            body["response_format"] = {"type": "json_object"}
        request = Request(base_url(self.settings.endpoint) + "/v1/chat/completions",
                          data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            return urlopen(request, timeout=self.settings.timeout)
        except HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            raise ModelError(f"LM Studio request failed ({exc.code}): {detail}") from exc
        except (OSError, URLError) as exc:
            raise ModelError(f"LM Studio is unavailable: {exc}") from exc
