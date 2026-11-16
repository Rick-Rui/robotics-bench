"""Harbor agent that runs the Qoder CLI (qodercli) in the task container.

Use it with `harbor run --agent qoder_harbor:QoderCli -m kmodel_latest`, with this
directory on PYTHONPATH. In /run, `agents=qoder` (or `qoder:<model>`) selects it.

Credential: QODER_PERSONAL_ACCESS_TOKEN in the host environment (the
QODER_PERSONAL_ACCESS_TOKEN repo secret). It is handed to each exec, never written
to the job config, and never printed.

What a trial records (logs/agent/):
  qodercli.jsonl      the CLI's stream-json output, one event per line, kept even
                      when the agent is killed at its timeout
  qodercli.stderr.txt the CLI's stderr
  trajectory.json     ATIF built from qodercli.jsonl after the run
  qoder-run.json      the model the CLI actually used, its version, credits, turns

The model check: Qoder resolves a display name to an internal id (on 2026-11-12
`Kimi-K3.1-Pro` -> `kmodel_latest`, a moving "latest" alias that cannot be pinned).
The id from the CLI's `system/init` event is compared with EXPECTED_MODEL_IDS; a
mismatch, a missing `result` event, or `is_error: true` raises, so the trial is an
execution error rather than a scored 0. Qoder reports zero tokens and bills in
credits, so token counts stay 0 and cost_usd stays unset; credits go in metadata.
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.installed.node_install import nvm_node_install_snippet
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)
from harbor.utils.trajectory_utils import format_trajectory_json

DEFAULT_VERSION = "1.1.87"
DEFAULT_MODEL = "kmodel_latest"  # Kimi K3.1 Pro; the display name is refused since 2026-11-16
TOKEN_ENV = "QODER_PERSONAL_ACCESS_TOKEN"
# Display name -> the id qodercli reports in system/init. Measured 2026-11-12, 1.1.87.
EXPECTED_MODEL_IDS = {
    "Kimi-K3.1-Pro": "kmodel_latest",
    "kmodel_latest": "kmodel_latest",
    "Qwen3.8-Max": "qmodel_38max",
    "GLM-5.3": "gmodel",
}
STREAM = "qodercli.jsonl"
STDERR = "qodercli.stderr.txt"
RUN_SUMMARY = "qoder-run.json"


def parse_stream(lines: list[str]) -> list[dict[str, Any]]:
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue      # a line cut off when the agent was killed
        if isinstance(event, dict):
            events.append(event)
    return events


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    return {
        "model_id": init.get("model"),
        "qodercli_version": init.get("qodercli_version"),
        "permission_mode": init.get("permissionMode"),
        "session_id": init.get("session_id"),
        "has_result": result is not None,
        "is_error": None if result is None else bool(result.get("is_error")),
        "subtype": None if result is None else result.get("subtype"),
        "num_turns": None if result is None else result.get("num_turns"),
        "duration_ms": None if result is None else result.get("duration_ms"),
        "total_credits": None if result is None else result.get("total_credits"),
    }


def check(summary: dict[str, Any], model: str) -> str | None:
    """Why this run must not be scored, or None."""
    expected = EXPECTED_MODEL_IDS.get(model)
    if summary["model_id"] is None:
        return "qodercli emitted no system/init event"
    if expected is not None and summary["model_id"] != expected:
        return f"qodercli used model id {summary['model_id']!r}, expected {expected!r} for {model!r}"
    if not summary["has_result"]:
        return "qodercli emitted no result event"
    if summary["is_error"]:
        return f"qodercli result is an error (subtype={summary['subtype']!r})"
    return None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(c.get("text", c.get("content", ""))) if isinstance(c, dict) else str(c) for c in content)
    return "" if content is None else json.dumps(content)


def to_trajectory(events: list[dict[str, Any]], instruction: str | None, model: str) -> Trajectory | None:
    """stream-json follows Claude Code's shape: assistant events carry thinking/text/tool_use
    parts, user events carry tool_result parts."""
    summary = summarize(events)
    steps: list[Step] = []
    if instruction:
        steps.append(Step(step_id=1, source="user", message=instruction))
    pending: dict[str, Step] = {}
    current: Step | None = None
    for event in events:
        kind = event.get("type")
        message = event.get("message") or {}
        if kind == "assistant":
            for part in message.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if current is None:
                    current = Step(step_id=len(steps) + 1, source="agent", message="",
                                   model_name=summary["model_id"] or model)
                    steps.append(current)
                if part.get("type") == "thinking":
                    current.reasoning_content = ((current.reasoning_content or "") + part.get("thinking", "")) or None
                elif part.get("type") == "text":
                    current.message = (current.message + ("\n" if current.message else "") + part.get("text", ""))
                elif part.get("type") == "tool_use":
                    call = ToolCall(tool_call_id=part.get("id") or "", function_name=part.get("name") or "",
                                    arguments=part.get("input") if isinstance(part.get("input"), dict) else {"input": part.get("input")})
                    current.tool_calls = (current.tool_calls or []) + [call]
                    pending[call.tool_call_id] = current
                    current = None      # the next assistant part starts a new step after the tool result
        elif kind == "user":
            for part in message.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    step = pending.pop(part.get("tool_use_id") or "", None)
                    if step is None:
                        continue
                    result = ObservationResult(source_call_id=part.get("tool_use_id"), content=_text(part.get("content")))
                    step.observation = Observation(results=(step.observation.results if step.observation else []) + [result])
            current = None
    if len(steps) <= (1 if instruction else 0):
        return None
    return Trajectory(
        session_id=summary["session_id"],
        agent=Agent(name="qoder", version=summary["qodercli_version"] or "unknown", model_name=summary["model_id"] or model),
        steps=steps,
        final_metrics=FinalMetrics(total_steps=len(steps)),
        extra={"qoder": summary},
    )


class QoderCli(BaseInstalledAgent):
    """Qoder CLI in print mode, all permissions granted inside the task container."""

    @staticmethod
    def name() -> str:
        return "qoder"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("version", DEFAULT_VERSION)
        super().__init__(*args, **kwargs)
        self._instruction: str | None = None

    def get_version_command(self) -> str | None:
        return "unset NPM_CONFIG_PREFIX; . ~/.nvm/nvm.sh; qodercli --version"

    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(environment, ("curl", "bash", "ca_certificates"))
        version = self._version or DEFAULT_VERSION
        await self.exec_as_agent(
            environment,
            command=(
                # nvm refuses to load while NPM_CONFIG_PREFIX is set, which some task
                # images do (same fix as the codex harness in isaac-model-comparison).
                "set -euo pipefail; unset NPM_CONFIG_PREFIX; "
                f"{nvm_node_install_snippet()} && "
                f"npm install -g --no-audit --no-fund @qoder-ai/qodercli@{shlex.quote(version)} && "
                "qodercli --version"
            ),
        )

    def _model(self) -> str:
        return (self.model_name or DEFAULT_MODEL).split("/", 1)[-1]

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            raise RuntimeError(f"{TOKEN_ENV} is not set on the host")
        self._instruction = instruction
        model = self._model()
        flags = ["-p", "-m", model, "-o", "stream-json", "--no-session-persistence",
                 "--dangerously-skip-permissions"]
        effort = os.environ.get("QODER_REASONING_EFFORT")
        if effort:
            flags += ["--reasoning-effort", effort]
        log_dir = str(self.environment_logs_dir)
        command = (
            "set -o pipefail; unset NPM_CONFIG_PREFIX; . ~/.nvm/nvm.sh; "
            f"qodercli {' '.join(shlex.quote(f) for f in flags)} -- {shlex.quote(instruction)} "
            f"2> {shlex.quote(log_dir + '/' + STDERR)} "
            f"| stdbuf -oL tee {shlex.quote(log_dir + '/' + STREAM)}"
        )
        await self.exec_as_agent(environment, command=command, env={TOKEN_ENV: token})
        # Read the stream back from the container: the host copy of logs/ is only
        # guaranteed after the trial, and the model check must fail this run now.
        out = await environment.exec(command=f"cat {shlex.quote(log_dir + '/' + STREAM)}")
        summary = summarize(parse_stream((out.stdout or "").splitlines()))
        problem = check(summary, model)
        if problem:
            raise RuntimeError(problem)

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = Path(self.logs_dir) / STREAM
        if not path.is_file():
            return
        events = parse_stream(path.read_text(errors="replace").splitlines())
        summary = summarize(events)
        summary["requested_model"] = self._model()
        summary["note"] = ("Qoder reports zero tokens and bills in credits; the model id is "
                           "Qoder's moving alias and cannot be pinned to a version.")
        try:
            (Path(self.logs_dir) / RUN_SUMMARY).write_text(json.dumps(summary, indent=2) + "\n")
        except OSError:
            pass
        context.metadata = {**(context.metadata or {}), "qoder": summary}
        try:
            trajectory = to_trajectory(events, self._instruction, self._model())
        except Exception as exc:      # a malformed stream must not hide the reward
            self.logger.debug(f"qoder trajectory conversion failed: {exc}")
            return
        if trajectory is not None:
            try:
                (Path(self.logs_dir) / "trajectory.json").write_text(format_trajectory_json(trajectory.to_json_dict()))
            except OSError:
                pass
