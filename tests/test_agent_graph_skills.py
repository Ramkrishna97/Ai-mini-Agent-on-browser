"""Offline coverage for graph turns and on-demand workspace skills."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from agent import run_agent
from agent_tools import ToolRuntime
from config import Settings


class AgentGraphSkillTests(unittest.TestCase):
    def test_selected_lmstudio_skill_folder_is_lazy_and_namespaced(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            external = root / "lm-skills"
            skill = external / "review" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("description: Review code.\nCheck tests first.\n", encoding="utf-8")
            async def check():
                async with ToolRuntime(root, extra_skill_root=str(external)) as runtime:
                    self.assertEqual(json.loads(await runtime.call("list_skills", {})),
                                     [{"name": "lmstudio:review", "description": "Review code."}])
                    self.assertIn("Check tests first.",
                                  await runtime.call("load_skill", {"name": "lmstudio:review"}))
            asyncio.run(check())

    def test_explicit_lmstudio_mcp_file_preserves_local_server(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "mcp.json"
            config.write_text('{"mcpServers":{}}', encoding="utf-8")
            async def check():
                async with ToolRuntime(root, mcp=True, extra_mcp_config=str(config)) as runtime:
                    self.assertIn("local", runtime.mcp_clients)
            asyncio.run(check())

    def test_skill_catalog_load_and_approval(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            skill = root / ".agent" / "skills" / "example" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\ndescription: Explain the task.\n---\nCheck output.\n", encoding="utf-8")
            async def check():
                async with ToolRuntime(root) as runtime:
                    names = {item["function"]["name"] for item in runtime.tools}
                    self.assertIn("list_skills", names)
                    self.assertIn("load_skill", names)
                    self.assertEqual(json.loads(await runtime.call("list_skills", {})),
                                     [{"name": "example", "description": "Explain the task."}])
                    self.assertIn("Check output.", await runtime.call("load_skill", {"name": "example"}))
                    with self.assertRaises(ValueError):
                        await runtime.call("load_skill", {"name": "../example"})
            asyncio.run(check())

    def test_graph_runs_skill_tool_then_answers_with_progress(self):
        class Engine:
            def __init__(self):
                self.calls = 0
                self.steps = []

            def set_target(self, target):
                pass

            def notify(self, phase, **values):
                if values.get("step"):
                    self.steps.append(values["step"])

            def chat_agent(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    self.assert_tool_available = any(x["function"]["name"] == "load_skill" for x in tools)
                    call = SimpleNamespace(id="skill-call", function=SimpleNamespace(
                        name="load_skill", arguments={"name": "example"}))
                    return SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call], raw={
                        "role": "assistant", "content": "", "tool_calls": [{"id": "skill-call", "type": "function",
                        "function": {"name": "load_skill", "arguments": {"name": "example"}}}]}))
                self.saw_skill = any(isinstance(x, dict) and x.get("role") == "tool" and "Check output." in x.get("content", "")
                                     for x in messages)
                return SimpleNamespace(message=SimpleNamespace(content="Completed.", tool_calls=[]))

        with TemporaryDirectory() as folder:
            root = Path(folder)
            skill = root / ".agent" / "skills" / "example" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("description: Explain the task.\nCheck output.\n", encoding="utf-8")
            engine = Engine()
            approvals = []
            answer = asyncio.run(run_agent(root, "Use the skill", Settings(), engine,
                                            lambda name, args: approvals.append(name) or True, lambda: None))
            self.assertEqual(answer, "Completed.")
            self.assertEqual(approvals, ["load_skill"])
            self.assertTrue(engine.assert_tool_available)
            self.assertTrue(engine.saw_skill)
            self.assertEqual(engine.calls, 2)
            self.assertTrue(any(step["label"].startswith("Agent turn") for step in engine.steps))


if __name__ == "__main__":
    unittest.main()
