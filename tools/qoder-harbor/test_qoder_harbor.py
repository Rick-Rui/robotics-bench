"""Offline tests for the Qoder harbor agent. Fixtures are real qodercli 1.1.87 stream-json
output (2026-11-12), with local paths replaced by /app and the init event trimmed.

Run: python3 -m unittest tools/qoder-harbor/test_qoder_harbor.py  (needs harbor installed)
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qoder_harbor as Q  # noqa: E402
from harbor.models.agent.context import AgentContext  # noqa: E402


def lines(name):
    return (HERE / "fixtures" / name).read_text().splitlines()


class FakeEnv:
    """Records commands; `cat` of the stream returns the given text."""
    def __init__(self, stream=""):
        self.stream, self.commands = stream, []
        self.default_user = None

    async def exec(self, command, **kw):
        self.commands.append((command, kw))
        return SimpleNamespace(return_code=0, stdout=self.stream if command.startswith("cat ") else "", stderr="")


class Parse(unittest.TestCase):
    def test_kimi_tool_run(self):
        s = Q.summarize(Q.parse_stream(lines("read_tool.jsonl")))
        self.assertEqual(s["model_id"], "kmodel_latest")
        self.assertEqual(s["qodercli_version"], "1.1.87")
        self.assertTrue(s["has_result"])
        self.assertFalse(s["is_error"])
        self.assertEqual(s["num_turns"], 2)
        self.assertIsNone(Q.check(s, "Kimi-K3.1-Pro"))

    def test_wrong_model_is_not_scored(self):
        s = Q.summarize(Q.parse_stream(lines("glm_no_tools.jsonl")))
        self.assertEqual(s["model_id"], "gmodel")
        self.assertIn("expected 'kmodel_latest'", Q.check(s, "Kimi-K3.1-Pro"))
        self.assertIsNone(Q.check(s, "GLM-5.3"))

    def test_killed_run_has_no_result(self):
        cut = lines("read_tool.jsonl")[:6] + ['{"type":"assistant","mess']   # killed mid-line
        s = Q.summarize(Q.parse_stream(cut))
        self.assertFalse(s["has_result"])
        self.assertEqual(Q.check(s, "Kimi-K3.1-Pro"), "qodercli emitted no result event")

    def test_error_result(self):
        evs = Q.parse_stream(lines("read_tool.jsonl"))
        evs[-1] = {**evs[-1], "is_error": True, "subtype": "error_during_execution"}
        self.assertIn("error_during_execution", Q.check(Q.summarize(evs), "Kimi-K3.1-Pro"))

    def test_no_init(self):
        self.assertEqual(Q.check(Q.summarize([]), "Kimi-K3.1-Pro"), "qodercli emitted no system/init event")

    def test_trajectory(self):
        t = Q.to_trajectory(Q.parse_stream(lines("read_tool.jsonl")), "read note.txt", "Kimi-K3.1-Pro")
        d = t.to_json_dict()
        self.assertEqual(d["agent"]["model_name"], "kmodel_latest")
        self.assertEqual(d["steps"][0]["source"], "user")
        calls = [s for s in d["steps"] if s.get("tool_calls")]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_calls"][0]["function_name"], "Read")
        self.assertIn("banana", calls[0]["observation"]["results"][0]["content"])
        self.assertEqual(d["steps"][-1]["message"], "banana")


class Agent(unittest.TestCase):
    def agent(self, td, model="Kimi-K3.1-Pro"):
        return Q.QoderCli(logs_dir=Path(td), model_name=model)

    def test_command_and_token(self):
        stream = "\n".join(lines("read_tool.jsonl"))
        with tempfile.TemporaryDirectory() as td:
            a, env = self.agent(td), FakeEnv(stream)
            os.environ[Q.TOKEN_ENV] = "pt-test"
            try:
                asyncio.run(a.run("do it; rm -rf / 'x'", env, AgentContext()))
            finally:
                del os.environ[Q.TOKEN_ENV]
            cmd, kw = env.commands[0]
            self.assertIn("qodercli -p -m Kimi-K3.1-Pro -o stream-json --no-session-persistence --dangerously-skip-permissions -- ", cmd)
            self.assertIn("'do it; rm -rf / '\"'\"'x'\"'\"''", cmd)        # instruction is one quoted argument
            self.assertIn("tee /logs/agent/qodercli.jsonl", cmd)
            self.assertNotIn("pt-test", cmd)                            # token only in env
            self.assertEqual(kw["env"][Q.TOKEN_ENV], "pt-test")

    def test_wrong_model_raises(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ[Q.TOKEN_ENV] = "pt-test"
            try:
                with self.assertRaisesRegex(RuntimeError, "gmodel"):
                    asyncio.run(self.agent(td).run("x", FakeEnv("\n".join(lines("glm_no_tools.jsonl"))), AgentContext()))
            finally:
                del os.environ[Q.TOKEN_ENV]

    def test_missing_token(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ.pop(Q.TOKEN_ENV, None)
            with self.assertRaisesRegex(RuntimeError, "not set"):
                asyncio.run(self.agent(td).run("x", FakeEnv(), AgentContext()))

    def test_post_run(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / Q.STREAM).write_text("\n".join(lines("read_tool.jsonl")) + "\n")
            a, ctx = self.agent(td), AgentContext()
            a.populate_context_post_run(ctx)
            self.assertEqual(ctx.metadata["qoder"]["model_id"], "kmodel_latest")
            self.assertIsNone(ctx.cost_usd)
            self.assertTrue((Path(td) / "trajectory.json").is_file())
            self.assertEqual(json.loads((Path(td) / Q.RUN_SUMMARY).read_text())["requested_model"], "Kimi-K3.1-Pro")


if __name__ == "__main__":
    unittest.main()
