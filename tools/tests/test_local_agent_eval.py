"""Tests for the dependency-free local Agent bridge."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


BRIDGE_PATH = Path(__file__).parents[1] / "local_agent_eval.py"
SPEC = importlib.util.spec_from_file_location("local_agent_eval", BRIDGE_PATH)
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


class LangGraphProjectLoaderTests(unittest.TestCase):
    """Exercise project discovery without installing LangGraph or calling an LLM."""

    def test_discovers_project_context_and_nested_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            package = project / "src" / "demo_agent"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (project / ".env").write_text("MODEL=test-model\n", encoding="utf-8")
            (project / "langgraph.json").write_text(
                json.dumps(
                    {
                        "dependencies": ["."],
                        "graphs": {"agent": "./src/demo_agent/graph.py:graph"},
                        "env": ".env",
                    }
                ),
                encoding="utf-8",
            )
            (package / "graph.py").write_text(
                """
class Context:
    def __init__(self, model="default"):
        self.model = model

class FakeGraph:
    context_schema = Context

    async def astream_events(self, inputs, **kwargs):
        assert kwargs["version"] == "v2"
        assert kwargs["context"].model == "test-model"
        yield {"event": "on_chain_start", "run_id": "root", "name": "agent", "parent_ids": [], "data": {"input": inputs}}
        yield {"event": "on_tool_start", "run_id": "tool", "name": "search", "parent_ids": ["root"], "data": {"input": {"query": "hello"}}}
        yield {"event": "on_tool_end", "run_id": "tool", "name": "search", "parent_ids": ["root"], "data": {"output": {"result": "world"}}}
        yield {"event": "on_chain_end", "run_id": "root", "name": "agent", "parent_ids": [], "data": {"output": {"messages": [{"content": "done"}]}}}

graph = FakeGraph()
""".strip(),
                encoding="utf-8",
            )

            project_dir, entrypoint, env_path, import_paths = BRIDGE.discover_langgraph_project(str(project), None)
            output, spans, event_count = asyncio.run(
                BRIDGE.collect_langgraph_events(
                    entrypoint,
                    "hello",
                    project_dir=project_dir,
                    import_paths=import_paths,
                    context_values={"model": "test-model"},
                )
            )

            self.assertEqual(env_path, project / ".env")
            self.assertEqual(output, "done")
            self.assertEqual(event_count, 4)
            self.assertEqual([span["kind"] for span in spans], ["CHAIN", "TOOL"])
            self.assertEqual(spans[1]["parent_span_id"], spans[0]["span_id"])
            self.assertEqual(spans[1]["input"], {"query": "hello"})
            self.assertEqual(spans[1]["output"], {"result": "world"})

    def test_loads_agent_executor_and_detects_input_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            package = project / "src" / "executor_agent"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "agent.py").write_text(
                """
class InputSchema:
    @classmethod
    def model_json_schema(cls):
        return {"type": "object", "properties": {"input": {"type": "string"}}}

class FakeExecutor:
    input_schema = InputSchema

    async def astream_events(self, inputs, **kwargs):
        assert inputs == {"input": "hello"}
        assert kwargs["config"]["configurable"]["thread_id"] == "thread-1"
        yield {"event": "on_chain_start", "run_id": "root", "name": "executor", "parent_ids": [], "data": {"input": inputs}}
        yield {"event": "on_chain_end", "run_id": "root", "name": "executor", "parent_ids": [], "data": {"output": {"output": "done"}}}

executor = FakeExecutor()
""".strip(),
                encoding="utf-8",
            )

            project_dir, entrypoint, env_path, import_paths = BRIDGE.discover_agent_project(
                str(project), "executor_agent.agent:executor", None
            )
            output, spans, event_count = asyncio.run(
                BRIDGE.collect_langgraph_events(
                    entrypoint,
                    "hello",
                    project_dir=project_dir,
                    import_paths=import_paths,
                    thread_id="thread-1",
                )
            )

            self.assertIsNone(env_path)
            self.assertEqual(output, "done")
            self.assertEqual(event_count, 2)
            self.assertEqual(spans[0]["input"], {"input": "hello"})

    def test_supports_factory_custom_input_output_and_invoke_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            module = project / "factory_agent.py"
            module.write_text(
                """
class FallbackAgent:
    name = "fallback-agent"

    def __init__(self, prefix):
        self.prefix = prefix

    async def ainvoke(self, inputs, **kwargs):
        assert inputs == {"payload": {"question": "hello"}}
        assert kwargs["config"]["metadata"]["suite"] == "smoke"
        return {"data": {"answer": self.prefix + "done"}}

def build_agent(prefix):
    return FallbackAgent(prefix)
""".strip(),
                encoding="utf-8",
            )

            output, spans, event_count = asyncio.run(
                BRIDGE.collect_langgraph_events(
                    "factory_agent:build_agent",
                    "hello",
                    project_dir=project,
                    import_paths=[project],
                    factory_values={"prefix": "result:"},
                    input_template={"payload": {"question": "{prompt}"}},
                    output_path="data.answer",
                    config_values={"metadata": {"suite": "smoke"}},
                )
            )

            self.assertEqual(output, "result:done")
            self.assertEqual(event_count, 0)
            self.assertEqual(len(spans), 1)
            self.assertEqual(spans[0]["metadata"]["capture_mode"], "invoke-fallback")


if __name__ == "__main__":
    unittest.main()
