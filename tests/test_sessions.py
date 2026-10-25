"""Saved conversations remain independent and reusable after an application restart."""

import json
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from config import Settings, WorkflowCancelled
from ui_server import Server, SessionStore


class SessionStoreTests(unittest.TestCase):
    def test_old_per_tool_approval_migrates_to_chat_scope(self):
        with TemporaryDirectory() as folder:
            store = SessionStore(folder)
            session_id = store.create()
            job = store.get(session_id)
            job.data.pop("always_allow_all")
            job.data["always_allowed_tools"] = ["write_file"]
            job.persist()

            reopened = SessionStore(folder).get(session_id)
            self.assertTrue(reopened.snapshot()["always_allow_all"])
            self.assertTrue(reopened.await_approval("browser", {"action": "navigate"}))

    def test_allow_always_survives_restart_only_in_its_conversation(self):
        with TemporaryDirectory() as folder:
            store = SessionStore(folder)
            approved_id = store.create()
            other_id = store.create()
            approved = store.get(approved_id)
            approved.data["always_allow_all"] = True
            approved.persist()
            other = store.get(other_id)
            other.persist()

            reopened = SessionStore(folder)
            self.assertTrue(reopened.get(approved_id).snapshot()["always_allow_all"])
            self.assertFalse(reopened.get(other_id).snapshot()["always_allow_all"])
            self.assertTrue(reopened.get(approved_id).await_approval("browser", {"action": "navigate"}))

    def test_allow_always_api_records_scope(self):
        with TemporaryDirectory() as folder, Server(("127.0.0.1", 0)) as server:
            server.sessions = SessionStore(folder)
            session_id = server.sessions.create()
            job = server.sessions.get(session_id)
            job.data["pending_tool"] = {"id": "pending-1", "name": "run_command",
                                        "arguments": {"command": "Write-Output first"}}
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                body = json.dumps({"id": "pending-1", "allowed": True, "always": True}).encode("utf-8")
                request = Request(server.origin + "/api/approve", body, headers={
                    "Content-Type": "application/json", "X-Workflow-Token": server.token,
                    "X-Session-Id": session_id}, method="POST")
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                self.assertTrue(SessionStore(folder).get(session_id).snapshot()["always_allow_all"])
            finally:
                server.shutdown()
                worker.join(5)

    def test_sessions_are_independent_and_restore_followup_context(self):
        with TemporaryDirectory() as folder:
            store = SessionStore(folder)
            first = store.create()
            second = store.create()
            self.assertNotEqual(first, second)
            self.assertEqual(store.list(), [])
            self.assertEqual(list(store.folder.glob("*.json")), [])
            store.get(second).data.update(status="succeeded", request="Second task")
            store.get(second).persist()
            job = store.get(first)
            job.chat_settings = Settings(model_name="test-model", context_tokens=32768, max_tokens=2048)
            job.data.update(status="succeeded", request="Build the first project", answer="Done",
                            path=folder, files=[], conversation=[{"role": "user", "text": "Build the first project"},
                                                                 {"role": "assistant", "text": "Done", "state": "done"}],
                            chat_ready=True, provider="ollama", model="test-model",
                            context_tokens=32768, max_tokens=2048)
            job.completed_context = {"request": "Build the first project", "answer": "Done",
                                     "files": [], "path": folder, "generation": ""}
            job.persist()

            reopened = SessionStore(folder)
            restored = reopened.get(first)
            self.assertEqual(restored.snapshot()["conversation"][-1]["text"], "Done")
            self.assertEqual(restored.chat_settings.context_tokens, 32768)
            self.assertEqual(restored.chat_settings.max_tokens, 2048)
            self.assertEqual(reopened.get(second).snapshot()["conversation"], [])

            class Engine:
                def health_check(self):
                    pass

                def set_target(self, target):
                    pass

                def generate(self, messages):
                    return "The first project is ready."

                def close(self):
                    pass

            with patch("ui_server.ensure_ollama"), patch("ui_server.create_engine", return_value=Engine()):
                self.assertTrue(restored.start_chat("What happened?"))
                for _ in range(100):
                    if not restored.worker_active:
                        break
                    time.sleep(0.01)
            self.assertEqual(restored.snapshot()["conversation"][-1]["text"], "The first project is ready.")
            self.assertEqual(SessionStore(folder).get(first).snapshot()["conversation"][-1]["text"],
                             "The first project is ready.")

            reopened.delete(second)
            self.assertEqual(len(reopened.list()), 1)
            with self.assertRaises(ValueError):
                reopened.get(second)

    def test_interrupted_job_is_not_restored_as_running(self):
        with TemporaryDirectory() as folder:
            store = SessionStore(folder)
            session_id = store.create()
            job = store.get(session_id)
            job.data.update(status="running", conversation=[{"role": "assistant", "text": "Partial", "state": "working"}])
            job.persist()
            restored = SessionStore(folder).get(session_id).snapshot()
            self.assertEqual(restored["status"], "failed")
            self.assertEqual(restored["conversation"][-1]["state"], "failed")

    def test_stop_interrupts_followup_generation_and_preserves_session(self):
        with TemporaryDirectory() as folder:
            store = SessionStore(folder)
            session_id = store.create()
            job = store.get(session_id)
            job.chat_settings = Settings()
            job.data.update(status="succeeded", chat_ready=True, request="Original task", answer="Done",
                            files=[], path=folder, conversation=[{"role": "user", "text": "Original task"},
                                                                  {"role": "assistant", "text": "Done", "state": "done"}])
            job.completed_context = {"request": "Original task", "answer": "Done", "files": [],
                                     "path": folder, "generation": ""}
            job.persist()

            class Engine:
                def health_check(self):
                    pass

                def set_target(self, target):
                    pass

                def generate(self, messages):
                    for _ in range(100):
                        job.progress({"phase": "generating", "delta": "token "})
                        time.sleep(0.01)
                    return "too late"

                def close(self):
                    pass

            with patch("ui_server.ensure_ollama"), patch("ui_server.create_engine", return_value=Engine()):
                self.assertTrue(job.start_chat("Explain more"))
                for _ in range(100):
                    if job.snapshot()["generation"].get("text"):
                        break
                    time.sleep(0.01)
                self.assertTrue(job.cancel())
                for _ in range(100):
                    if not job.worker_active:
                        break
                    time.sleep(0.01)
            self.assertEqual(job.snapshot()["status"], "cancelled")
            self.assertTrue(job.snapshot()["chat_ready"])
            self.assertEqual(SessionStore(folder).get(session_id).snapshot()["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
