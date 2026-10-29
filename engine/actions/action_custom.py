"""Custom action handler."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Self

from engine.type_defs import ActionConfig, ExecutionCommand, JsonObject

from .base import AgentActionHandler


class action_custom(AgentActionHandler):
    name = "custom"
    parameters = frozenset({"prompt"})
    required_parameters = frozenset({"prompt"})

    def run(
        self: Self,
        config: ActionConfig,
        execute: Callable[[ExecutionCommand], int],
        output_path: Path,
    ) -> JsonObject:
        context = self.context(config)
        prompt = context + (
            "Apply the original user command to this task only. Follow the Cyber Bench guide's model-role rules. "
            "Never author instruction.md or writeup.md yourself; required edits must be delegated to a fresh first-party Muse session from the original approved sources. "
            "Do not relax protected-authoring boundaries or claim validation that was not run."
        )
        self.execute_agent(config, execute, prompt)
        return {"agent": config["execution"]["agent"]}
