"""LM Studio catalog, endpoint, and generation contract tests."""

import io
import json
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from config import Settings, create_engine
from lmstudio_engine import LMStudioEngine, list_models, load_model, unload_model
from ui_server import LMStudioPreloader, Server, SessionStore


class LMStudioTests(unittest.TestCase):
    def test_native_catalog_excludes_embedding_models(self):
        payload = {"models": [
            {"key": "coder/7b", "display_name": "Coder 7B", "type": "llm",
             "size_bytes": 123, "loaded_instances": [],
             "capabilities": {"trained_for_tool_use": True}},
            {"key": "embed/1b", "type": "embedding", "loaded_instances": []},
        ]}
        with patch("lmstudio_engine.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as open_url:
            models = list_models("http://localhost:1234/v1")
        self.assertEqual(open_url.call_args.args[0], "http://localhost:1234/api/v1/models")
        self.assertEqual(models[0]["name"], "coder/7b")
        self.assertTrue(models[0]["can_generate"])
        self.assertFalse(models[1]["can_generate"])

    def test_local_endpoint_and_ten_minute_ttl(self):
        with self.assertRaises(ValueError):
            Settings(provider="lmstudio", endpoint="http://remote.example:1234")
        settings = Settings(provider="lmstudio", endpoint="http://localhost:1234", model_name="coder/7b")
        engine = create_engine(settings)
        self.assertIsInstance(engine, LMStudioEngine)
        with patch("lmstudio_engine.urlopen", return_value=io.BytesIO(b"{}")) as open_url:
            engine._request([{"role": "user", "content": "Hi"}], tools=[{"type": "function"}])
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:1234/v1/chat/completions")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "coder/7b")
        self.assertEqual(body["ttl"], 600)
        self.assertEqual(body["tools"], [{"type": "function"}])

    def test_native_load_and_unload_use_selected_context_and_instance(self):
        with patch("lmstudio_engine.urlopen", return_value=io.BytesIO(b'{"instance_id":"owned-1"}')) as open_url:
            self.assertEqual(load_model("http://localhost:1234/v1", "coder/7b", 32768), "owned-1")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:1234/api/v1/models/load")
        self.assertEqual(json.loads(request.data), {"model": "coder/7b", "context_length": 32768})
        with patch("lmstudio_engine.urlopen", return_value=io.BytesIO(b"{}")) as open_url:
            unload_model("http://localhost:1234", "owned-1")
        self.assertEqual(json.loads(open_url.call_args.args[0].data), {"instance_id": "owned-1"})

    def test_owned_instance_expires_after_ten_idle_minutes_only(self):
        unloaded = []
        active = [True]
        preloader = LMStudioPreloader(active_check=lambda: active[0],
                                     loader=lambda *_: "owned-1",
                                     unloader=lambda endpoint, instance: unloaded.append((endpoint, instance)))
        preloader.select("http://localhost:1234", "coder/7b", 32768)
        for _ in range(100):
            if not preloader.snapshot()["busy"]:
                break
            threading.Event().wait(0.01)
        self.assertTrue(preloader.snapshot()["loaded"])
        self.assertFalse(preloader.expire(float("inf")))
        active[0] = False
        self.assertTrue(preloader.expire(float("inf")))
        for _ in range(100):
            if not preloader.snapshot()["busy"]:
                break
            threading.Event().wait(0.01)
        self.assertEqual(unloaded, [("http://localhost:1234", "owned-1")])
        self.assertFalse(preloader.snapshot()["loaded"])

    def test_ide_catalog_endpoint_uses_selected_local_server(self):
        with TemporaryDirectory() as folder, \
             patch("ui_server.SessionStore", side_effect=lambda: SessionStore(folder)), \
             patch("lmstudio_engine.list_models", return_value=[{"name": "coder/7b", "can_generate": True}]) as catalog:
            with Server(("127.0.0.1", 0)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{server.server_port}"
                    with urlopen(base + "/api/session") as response:
                        session = json.load(response)
                    self.assertEqual(session["version"], 19)
                    request = Request(base + "/api/lmstudio/models", method="POST",
                                      data=b'{"endpoint":"http://localhost:1234"}',
                                      headers={"Content-Type": "application/json",
                                               "X-Workflow-Token": session["token"],
                                               "X-Session-Id": session["id"]})
                    with urlopen(request) as response:
                        self.assertEqual(json.load(response)["models"][0]["name"], "coder/7b")
                    catalog.assert_called_once_with("http://localhost:1234")
                finally:
                    server.shutdown()
                    thread.join(2)

    def test_ide_load_endpoint_tracks_owned_instance(self):
        with Server(("127.0.0.1", 0)) as server:
            server.lmstudio_preloader = LMStudioPreloader(loader=lambda *_: "owned-2", unloader=lambda *_: None)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(server.origin + "/api/session") as response:
                    session = json.load(response)
                request = Request(server.origin + "/api/lmstudio/load", method="POST",
                                  data=json.dumps({"endpoint": "http://localhost:1234", "model": "coder/7b",
                                                   "context_tokens": 32768}).encode(),
                                  headers={"Content-Type": "application/json",
                                           "X-Workflow-Token": session["token"],
                                           "X-Session-Id": session["id"]})
                with urlopen(request) as response:
                    self.assertEqual(response.status, 202)
                for _ in range(100):
                    if not server.lmstudio_preloader.snapshot()["busy"]:
                        break
                    threading.Event().wait(0.01)
                self.assertEqual(server.lmstudio_preloader.snapshot()["context_length"], 32768)
                self.assertTrue(server.lmstudio_preloader.snapshot()["loaded"])
            finally:
                server.shutdown()
                thread.join(2)


if __name__ == "__main__":
    unittest.main()
