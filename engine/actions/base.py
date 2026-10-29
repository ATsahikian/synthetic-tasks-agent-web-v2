"""The two action lifecycles and shared agent execution support."""

import copy
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

from .common import agent_command, validate_parameters
from .definitions import canonical_json
from ..errors import EngineError


class ActionHandler(ABC):
    parameters = frozenset()
    accepts_arbitrary_parameters = False
    required_parameters = frozenset()
    runner_fields = frozenset()

    def configure_runner(self, runner, directory):
        if not isinstance(runner, dict):
            raise EngineError("runner must be an object.")
        if set(runner) - self.runner_fields:
            raise EngineError(f"Unknown runner fields for {self.name}.")
        return copy.deepcopy(runner)

    def prepare(self, runner, parameters):
        validate_parameters(self.name, parameters, self.parameters, self.required_parameters)
        return json.loads(canonical_json(runner))

    @abstractmethod
    def run(self, config, execute, output_path):
        """Return the result, or raise an exception to report a failed completion."""


class SyncActionHandler(ActionHandler):
    """Run inline and emit a single completed event containing the result or error."""

    mode = "sync"


class AsyncActionHandler(ActionHandler):
    """Persist started, run in a detached worker, then persist completed."""

    mode = "async"


class AgentActionHandler(AsyncActionHandler):
    needs_guide = True

    def prepare(self, runner, parameters):
        spec = super().prepare(runner, parameters)
        agent = parameters.get("agent", "codex")
        if not isinstance(agent, str) or agent not in {"codex", "claude", "muse"}:
            raise EngineError("agent must be codex, claude or muse.")
        spec["agent"] = agent
        spec["agent_bin"] = os.environ.get(f"{agent.upper()}_BIN", agent)
        spec["templates_path"] = os.environ.get(
            "CLAUDE_TEMPLATES_PATH", str(Path.home() / ".codex/plugins/cache/claude-templates")
        )
        if self.needs_guide:
            guide = Path(os.environ.get(
                "CYBER_BENCH_GUIDE_PATH",
                str(Path(spec["templates_path"]) / "team-aai/0.0.0/skills/cyber-bench-guide/SKILL.md"),
            ))
            try:
                spec["guide"] = guide.read_text("utf-8")
            except OSError as error:
                raise EngineError(
                    f"Cannot read Cyber Bench guide; set CYBER_BENCH_GUIDE_PATH: {error}"
                ) from error
        return spec

    def context(self, config):
        task_path = Path(config["task_path"])
        context = (
            f"This invocation owns exactly one task: {task_path}. Task set: {task_path.parent}. "
            "Never edit a sibling task. Runtime state and events are owned by the engine; do not edit them.\n"
            f"Original user request:\n{config['action']['parameters'].get('prompt', '')}\n"
        )
        if config["execution"].get("guide"):
            context += (
                "\nRead and follow this source of truth directly; do not invoke the installed skill.\n"
                f"<cyber-bench-guide>\n{config['execution']['guide']}\n</cyber-bench-guide>\n"
            )
        return context

    def execute_agent(self, config, execute, prompt):
        command = agent_command(config["execution"], Path(config["task_path"]), prompt)
        command.update({"cwd": config["task_path"], "timeout_ms": 3600000, "literal": True})
        code = execute(command)
        if code != 0:
            raise RuntimeError(f"{config['execution']['agent']} exited with status {code}.")
