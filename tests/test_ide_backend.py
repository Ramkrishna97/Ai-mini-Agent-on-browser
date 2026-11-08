"""Backend contracts shared by the browser and both IDE adapters."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

from agent_tools import ToolRuntime
from config import OllamaEngine, Settings
from ui_server import JobState, OllamaPreloader, Server, SessionStore, record_turn_log, record_turn_step
from workspace_context import workspace_overview


class IdeBackendTests(unittest.TestCase):
    def test_activity_proof_stays_with_its_reply_and_is_bounded(self):
        state = JobState()
        state.data["conversation"] = [{"role": "user", "text": "Run tests"},
                                      {"role": "assistant", "text": "", "state": "working"}]
        state.progress({"step": {"label": "Command result", "detail": "> pytest\nExit code: 0\n141 passed"}})
        record_turn_log(state.data, "Test suite completed")
        first = state.snapshot()["conversation"][1]
        self.assertIn("141 passed", first["steps"][0]["detail"])
        self.assertEqual(first["logs"], ["Test suite completed"])
        state.data["conversation"].extend([{"role": "user", "text": "Next"},
                                           {"role": "assistant", "text": "", "state": "working"}])
        for index in range(40):
            record_turn_step(state.data, {"label": str(index), "detail": "x" * 2100})
            record_turn_log(state.data, str(index))
        second = state.snapshot()["conversation"][3]
        self.assertEqual(len(second["steps"]), 32)
        self.assertEqual(len(second["logs"]), 40)
        self.assertEqual(len(second["steps"][-1]["detail"]), 2000)
        self.assertEqual(len(state.snapshot()["conversation"][1]["steps"]), 1)

    def test_ide_chat_followup_rejects_explicit_run_command(self):
        with self.assertRaisesRegex(ValueError, "Chat mode cannot run commands"):
            JobState().start_chat("/run whoami", chat_only=True)

    def test_first_ide_chat_is_read_only_and_does_not_enter_agent_loop(self):
        with TemporaryDirectory() as folder, patch("ui_server.SessionStore", side_effect=lambda: SessionStore(folder)):
            root = Path(folder) / "project"
            root.mkdir()
            calls = []

            class Engine:
                def __init__(self, progress):
                    self.progress = progress

                def health_check(self):
                    pass

                def supports_tools(self):
                    return False

                def generate(self, messages, **kwargs):
                    calls.append((messages, kwargs))
                    return "I can explain this without changing the project."

            with patch("ui_server.ensure_ollama", return_value="http://127.0.0.1:11434"), \
                 patch("ui_server.create_engine", side_effect=lambda settings, progress: Engine(progress)), \
                 patch("ui_server.run_agent", side_effect=AssertionError("Chat must not enter agent mode")):
                with Server(("127.0.0.1", 0)) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    base = f"http://127.0.0.1:{server.server_port}"
                    try:
                        with urlopen(base + "/api/session") as response:
                            session = json.load(response)
                        request = Request(base + "/api/run", method="POST",
                                          data=json.dumps({"path": str(root), "instruction": "Explain this project",
                                                           "name": "", "provider": "ollama", "model": "test:latest",
                                                           "chat_only": True, "thinking_effort": "high", "mcp": True}).encode(),
                                          headers={"Content-Type": "application/json",
                                                   "X-Workflow-Token": session["token"],
                                                   "X-Session-Id": session["id"]})
                        with urlopen(request) as response:
                            self.assertEqual(response.status, 202)
                        for _ in range(100):
                            with urlopen(Request(base + "/api/status", headers={"X-Session-Id": session["id"]})) as response:
                                status = json.load(response)
                            if status["status"] != "running":
                                break
                            time.sleep(0.01)
                        self.assertEqual(status["status"], "succeeded", status.get("error"))
                        self.assertEqual(status["mode"], "chat")
                        self.assertEqual(status["files"], [])
                        self.assertEqual(len(calls), 1)
                        self.assertIn("no file, command", calls[0][0][0]["content"])
                        self.assertEqual(calls[0][1], {"no_thinking": True})
                        self.assertFalse(status["mcp"])
                    finally:
                        server.shutdown()
                        thread.join(2)

    def test_thinking_level_and_idle_residency_reach_ollama(self):
        calls = []

        class Client:
            def chat(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(message=SimpleNamespace(content="Done"), done_reason="stop",
                                       eval_count=2, eval_duration=1_000_000)

        settings = Settings(thinking_effort="high")
        self.assertEqual(OllamaEngine(settings, client=Client()).generate([{"role": "user", "content": "Hi"}]), "Done")
        self.assertEqual(calls[0]["think"], "high")
        self.assertEqual(calls[0]["keep_alive"], "10m")
        with self.assertRaises(ValueError):
            Settings(thinking_effort="invalid")

    def test_idle_engine_cleanup_never_interrupts_active_work(self):
        state = JobState()
        closed = []
        state.active_engine = SimpleNamespace(close=lambda: closed.append(True))
        state.last_model_activity = time.monotonic() - 601
        state.worker_active = True
        self.assertFalse(state.expire_idle_engine())
        self.assertEqual(closed, [])
        state.worker_active = False
        self.assertTrue(state.expire_idle_engine())
        self.assertEqual(closed, [True])
        self.assertFalse(state.expire_idle_engine())

    def test_preloader_expires_display_after_ten_idle_minutes(self):
        preloader = OllamaPreloader(active_check=lambda: False)
        preloader.loaded = preloader.selected = "example:latest"
        preloader.phase = "ready"
        preloader.expires_at = time.monotonic() - 1
        self.assertEqual(preloader.snapshot()["loaded"], "")
        self.assertEqual(preloader.snapshot()["selected"], "example:latest")

    def test_workspace_overview_and_plan_are_bounded(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]", encoding="utf-8")
            overview = json.loads(workspace_overview(root))
            self.assertEqual(overview["root"], str(root.resolve()))
            self.assertIn("src/main.py", overview["entries"])
            self.assertIn("pyproject.toml", overview["manifests"])
            plans = []

            async def check():
                async with ToolRuntime(root, on_plan=plans.append) as runtime:
                    await runtime.call("set_plan", {"steps": ["Inspect", "Fix", "Test"], "current": 1})

            import asyncio
            asyncio.run(check())
            self.assertEqual(plans, [{"steps": ["Inspect", "Fix", "Test"], "current": 1}])

    def test_health_endpoint_does_not_create_a_conversation(self):
        with TemporaryDirectory() as folder, patch("ui_server.SessionStore", side_effect=lambda: SessionStore(folder)):
            with Server(("127.0.0.1", 0)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with urlopen(f"http://127.0.0.1:{server.server_port}/api/health") as response:
                        health = json.load(response)
                    self.assertEqual(health["idle_offload_seconds"], 600)
                    self.assertEqual(list(Path(folder).iterdir()), [])
                finally:
                    server.shutdown()
                    thread.join(2)

    def test_ide_api_uses_project_root_and_reports_plan(self):
        with TemporaryDirectory() as folder, patch("ui_server.SessionStore", side_effect=lambda: SessionStore(folder)):
            root = Path(folder) / "project"
            root.mkdir()

            class Engine:
                def __init__(self, progress):
                    self.progress = progress

                def health_check(self):
                    pass

                def notify(self, phase, **values):
                    self.progress({"phase": phase, **values})

                def close(self):
                    pass

            observed = []

            async def fake_agent(workspace, instruction, settings, engine, *_args, **_kwargs):
                observed.append((workspace, settings.thinking_effort))
                engine.notify("tool", plan={"steps": ["Inspect project", "Run tests"], "current": 0})
                return "Ready."

            with patch("ui_server.ensure_ollama", return_value="http://127.0.0.1:11434"), \
                 patch("ui_server.create_engine", side_effect=lambda settings, progress: Engine(progress)), \
                 patch("ui_server.run_agent", side_effect=fake_agent):
                with Server(("127.0.0.1", 0)) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    base = f"http://127.0.0.1:{server.server_port}"
                    try:
                        with urlopen(base + "/api/session") as response:
                            session = json.load(response)
                        request = Request(base + "/api/run", method="POST",
                                          data=json.dumps({"path": str(root), "instruction": "Inspect this project",
                                                           "name": "", "provider": "ollama", "model": "test:latest",
                                                           "agent_mode": True, "thinking_effort": "high"}).encode(),
                                          headers={"Content-Type": "application/json",
                                                   "X-Workflow-Token": session["token"],
                                                   "X-Session-Id": session["id"]})
                        with urlopen(request) as response:
                            self.assertEqual(response.status, 202)
                        for _ in range(100):
                            status_request = Request(base + "/api/status", headers={"X-Session-Id": session["id"]})
                            with urlopen(status_request) as response:
                                status = json.load(response)
                            if status["status"] != "running":
                                break
                            time.sleep(0.01)
                        self.assertEqual(status["status"], "succeeded", status.get("error"))
                        self.assertEqual(status["plan"]["steps"], ["Inspect project", "Run tests"])
                        self.assertEqual(observed, [(root, "high")])
                    finally:
                        server.shutdown()
                        thread.join(2)


if __name__ == "__main__":
    unittest.main()
