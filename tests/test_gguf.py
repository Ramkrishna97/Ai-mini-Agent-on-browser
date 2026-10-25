"""Offline GGUF backend contract checks (no model download needed)."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from agent import run_agent
from config import Settings, WorkflowCancelled, create_engine
from gguf_engine import GgufEngine, validate_gguf


class FakeLlamaHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        self.server.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        request = self.server.requests[-1]
        if request["stream"] and request.get("tools") and len(self.server.requests) == 1:
            intro = (b'' if request["messages"][-1]["content"] == "Silent tool" else
                     b'data: {"choices":[{"delta":{"content":"Checking files..."},"finish_reason":null}]}\n\n')
            body = (intro +
                    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"list_","arguments":"{\\"path\\":"}}]},"finish_reason":null}]}\n\n'
                    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"files","arguments":"\\".\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'
                    b'data: [DONE]\n\n')
            content_type = "text/event-stream"
        elif request["stream"] and request.get("tools"):
            body = (b'data: {"choices":[{"delta":{"content":"Do"},"finish_reason":null}]}\n\n'
                    b'data: {"choices":[{"delta":{"content":"ne"},"finish_reason":"stop"}]}\n\n'
                    b'data: [DONE]\n\n')
            content_type = "text/event-stream"
        elif request["stream"]:
            body = (b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
                    b'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n'
                    b'data: [DONE]\n\n')
            content_type = "text/event-stream"
        else:
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Done", "tool_calls": []}}]}).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class GgufTests(unittest.TestCase):
    def test_gguf_header_is_required(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "model.gguf"
            path.write_bytes(b"not a model")
            with self.assertRaises(ValueError):
                validate_gguf(str(path))
            path.write_bytes(b"GGUF\x03\x00\x00\x00")
            self.assertEqual(validate_gguf(str(path)), path)

    def fake_engine(self, server):
        settings = Settings(provider="gguf", model_name=str(Path.cwd() / "model.gguf"))
        engine = create_engine(settings)
        self.assertIsInstance(engine, GgufEngine)
        engine.process = type("Running", (), {"poll": lambda self: None})()
        engine.base_url = f"http://127.0.0.1:{server.server_port}"
        return engine

    def test_streamed_generation(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaHandler) as server:
            server.requests = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                engine = self.fake_engine(server)
                self.assertEqual(engine.generate([{"role": "user", "content": "hello"}], json_output=True), "hello world")
                self.assertEqual(server.requests[0]["response_format"], {"type": "json_object"})
            finally:
                engine.process = None
                server.shutdown()
                thread.join()

    def test_loading_checks_stop_request_before_model_is_ready(self):
        with TemporaryDirectory() as folder:
            model = Path(folder) / "model.gguf"
            model.write_bytes(b"GGUF\x03\x00\x00\x00")
            events = []

            def progress(event):
                events.append(event)
                if len(events) == 2:
                    raise WorkflowCancelled("Stopped")

            class Process:
                def __init__(self):
                    self.stopped = False

                def poll(self):
                    return 0 if self.stopped else None

                def terminate(self):
                    self.stopped = True

                def wait(self, timeout=None):
                    return 0

            process = Process()
            engine = GgufEngine(Settings(provider="gguf", model_name=str(model)), progress=progress)
            with patch("gguf_engine.ensure_llama_server", return_value=Path(__file__)), \
                 patch("gguf_engine.subprocess.Popen", return_value=process), \
                 patch("gguf_engine.urlopen", side_effect=URLError("not ready")):
                with self.assertRaises(WorkflowCancelled):
                    engine.health_check()
            engine.close()
            self.assertTrue(process.stopped)
            self.assertEqual([event["phase"] for event in events], ["loading", "loading"])

    def test_agent_tool_result_uses_openai_call_id(self):
        with TemporaryDirectory() as folder, ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaHandler) as server:
            server.requests = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                engine = self.fake_engine(server)
                events = []
                engine.progress = events.append
                answer = asyncio.run(run_agent(Path(folder), "List files", engine.settings, engine,
                                               lambda *_: True, lambda: None))
                self.assertEqual(answer, "Done")
                self.assertTrue(any(event.get("delta") == "Checking files..." for event in events))
                self.assertTrue(any(event.get("activity") == "Running tool: list_files" for event in events))
                self.assertEqual(server.requests[1]["messages"][-1]["tool_call_id"], "call-1")
                self.assertEqual(server.requests[1]["messages"][-1]["role"], "tool")
                self.assertNotIn("tool_name", server.requests[1]["messages"][-1])
            finally:
                engine.process = None
                server.shutdown()
                thread.join()

    def test_silent_tool_call_reports_activity_without_text(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaHandler) as server:
            server.requests = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                engine = self.fake_engine(server)
                events = []
                engine.progress = events.append
                response = engine.chat_agent([{"role": "user", "content": "Silent tool"}],
                                             [{"type": "function", "function": {"name": "list_files"}}])
                self.assertEqual(response.message.content, "")
                self.assertEqual(response.message.tool_calls[0].function.name, "list_files")
                self.assertTrue(any(event.get("activity") == "Preparing tool request" for event in events))
                self.assertEqual(events[-1]["phase"], "tool")
            finally:
                engine.process = None
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
