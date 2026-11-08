"""Offline checks for the Intel NPU engine's agent integration."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from config import Settings, create_engine
from npu_engine import NpuEngine, list_npu_models, validate_npu_model
import npu_engine


class FakeHistory(list):
    def set_tools(self, tools):
        self.tools = tools


class NpuTests(unittest.TestCase):
    def test_engine_selection_and_tool_history(self):
        settings = Settings(provider="npu", model_name="C:/test/model", context_tokens=4096,
                            max_tokens=512, chunk_chars=1000)
        engine = create_engine(settings)
        self.assertIsInstance(engine, NpuEngine)
        fake_genai = ModuleType("openvino_genai")
        fake_genai.ChatHistory = FakeHistory
        with patch.dict(sys.modules, {"openvino_genai": fake_genai}):
            history = engine._history([
                {"role": "user", "content": "List the files"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "list_files", "arguments": {"path": "."}}}]},
                {"role": "tool", "tool_name": "list_files", "content": '["hello.txt"]'},
            ], [{"function": {"name": "list_files"}}])
        self.assertEqual(history.tools[0]["function"]["name"], "list_files")
        self.assertIn("list_files", history[1]["content"])
        self.assertEqual(history[-1]["role"], "user")
        self.assertIn("hello.txt", history[-1]["content"])

    def test_dynamic_catalog_accepts_complete_models_only(self):
        with TemporaryDirectory() as root:
            library = Path(root)
            complete = library / "model-a"
            complete.mkdir()
            for name in npu_engine.REQUIRED:
                (complete / name).write_bytes(b"x")
            (library / "incomplete").mkdir()
            with patch.object(npu_engine, "MODEL_ROOT", library):
                models = list_npu_models()
                self.assertEqual(len(models), 1)
                self.assertEqual(models[0]["path"], str(complete.resolve()))
                self.assertTrue(models[0]["auto_eligible"])
                self.assertEqual(validate_npu_model(str(complete)), complete.resolve())
                with self.assertRaises(ValueError):
                    validate_npu_model(str(library / "incomplete"))
                with self.assertRaises(ValueError):
                    validate_npu_model(root)

    def test_import_rejects_oversized_model_before_download(self):
        hub = ModuleType("huggingface_hub")
        siblings = [SimpleNamespace(rfilename=name, size=1_600_000_000 if name.endswith("model.bin") else 1)
                    for name in npu_engine.REQUIRED]
        hub.HfApi = lambda: SimpleNamespace(model_info=lambda *args, **kwargs:
                                           SimpleNamespace(siblings=siblings))
        downloads = []
        hub.snapshot_download = lambda *args, **kwargs: downloads.append(args)
        with patch.dict(sys.modules, {"huggingface_hub": hub}):
            with self.assertRaisesRegex(ValueError, "1.5 GB"):
                npu_engine.import_npu_model("example/oversized-model")
        self.assertEqual(downloads, [])


if __name__ == "__main__":
    unittest.main()
