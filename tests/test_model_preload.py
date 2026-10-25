"""Ollama model selection keeps only the latest app-selected model resident."""

from io import BytesIO
import json
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ui_server import OllamaPreloader, ensure_ollama, ollama_model_location_hint


class FakeClient:
    def __init__(self):
        self.lock = threading.Lock()
        self.loaded = set()
        self.calls = []
        self.first_started = threading.Event()
        self.release_first = threading.Event()

    def list(self):
        return SimpleNamespace(models=[SimpleNamespace(model=name) for name in ("first:latest", "second:latest")])

    def show(self, model):
        return SimpleNamespace(capabilities=["completion"])

    def ps(self):
        with self.lock:
            return SimpleNamespace(models=[SimpleNamespace(model=name) for name in self.loaded])

    def generate(self, *, model, prompt, keep_alive):
        self.calls.append((model, prompt, keep_alive))
        if model == "first:latest" and keep_alive == -1:
            self.first_started.set()
            if not self.release_first.wait(2):
                raise TimeoutError("Test model load was not released.")
        with self.lock:
            if keep_alive == 0:
                self.loaded.discard(model)
            else:
                self.loaded.add(model)


class ModelPreloadTests(unittest.TestCase):
    def wait_for(self, manager, phase):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = manager.snapshot()
            if not state["busy"] and state["phase"] == phase:
                return state
            time.sleep(0.01)
        self.fail(f"Preloader did not reach {phase}: {manager.snapshot()}")

    def test_switch_during_load_unloads_old_model_then_warms_new_one(self):
        client = FakeClient()
        manager = OllamaPreloader(client_factory=lambda: client, ready=lambda: None)
        manager.select("first:latest")
        self.assertTrue(client.first_started.wait(1))
        manager.select("second:latest")
        client.release_first.set()
        self.assertEqual(self.wait_for(manager, "ready")["loaded"], "second:latest")
        self.assertEqual(client.calls, [("first:latest", "", -1), ("first:latest", "", 0),
                                        ("second:latest", "", -1)])
        manager.offload("second:latest")
        off = self.wait_for(manager, "off")
        self.assertEqual(off["loaded"], "")
        self.assertTrue(off["cleared"])
        self.assertEqual(client.calls[-1], ("second:latest", "", 0))

    def test_offload_selected_model_even_if_loaded_before_app_started(self):
        client = FakeClient()
        client.loaded.add("second:latest")
        manager = OllamaPreloader(client_factory=lambda: client, ready=lambda: None)
        manager.offload("second:latest")
        self.wait_for(manager, "off")
        self.assertEqual(client.calls, [("second:latest", "", 0)])

    def test_rejects_cloud_model(self):
        manager = OllamaPreloader(client_factory=FakeClient, ready=lambda: None)
        with self.assertRaises(ValueError):
            manager.select("remote:cloud")

    def test_empty_model_location_diagnostic_does_not_change_environment(self):
        with TemporaryDirectory() as folder, patch.dict("os.environ", {"OLLAMA_MODELS": folder}):
            hint = ollama_model_location_hint()
            self.assertIn("has no Ollama manifests", hint)
            self.assertIn("does not change Ollama's model directory", hint)
            self.assertEqual(list(Path(folder).iterdir()), [])
            manifest = Path(folder) / "manifests-v2" / "example"
            manifest.parent.mkdir()
            manifest.write_text("{}", encoding="utf-8")
            self.assertIn("contains Ollama manifests", ollama_model_location_hint())

    def test_existing_server_with_models_wins_over_empty_loopback_server(self):
        class Response(BytesIO):
            status = 200

        def fake_urlopen(url, timeout):
            names = ["saved:latest"] if "[::1]" in url else []
            return Response(json.dumps({"models": [{"name": name} for name in names]}).encode())

        with patch("ui_server.urlopen", side_effect=fake_urlopen):
            self.assertEqual(ensure_ollama(), "http://[::1]:11434")


if __name__ == "__main__":
    unittest.main()
