"""Implementation action handler."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Self

from engine.type_defs import ActionConfig, ExecutionCommand, ExecutionSpec, JsonObject

from .base import AgentActionHandler
from .common import design_sources, digest, require_source, result_marker


class action_impl(AgentActionHandler):
    name = "impl"
    parameters = frozenset({"prompt", "reference_task", "source"})
    required_parameters = frozenset({"source"})

    def prepare(
        self: Self, runner: ExecutionSpec, parameters: JsonObject
    ) -> ExecutionSpec:
        spec = super().prepare(runner, parameters)
        spec["skill"] = (
            Path(__file__).resolve().parent.parent
            / "skills/implement-task-and-oracle.md"
        ).read_text("utf-8")
        return spec

    def run(
        self: Self,
        config: ActionConfig,
        execute: Callable[[ExecutionCommand], int],
        output_path: Path,
    ) -> JsonObject:
        action, spec = config["action"], config["execution"]
        parameters = action["parameters"]
        task_path = Path(config["task_path"])
        sources = design_sources(task_path)
        design_path, template_path = (
            task_path / "design.md",
            task_path.parent / "template.md",
        )
        require_source(config, approved=True)
        context = self.context(config)
        if parameters.get("reference_task"):
            context += (
                f"Explicit reference task (read only): {parameters['reference_task']}\n"
            )
        prompt = context + "\n" + spec["skill"]
        self.execute_agent(config, execute, prompt)
        result = result_marker(output_path)
        if result.get("oracle_passed") is not True:
            raise RuntimeError(
                "Implementation did not emit a verified passing oracle result."
            )
        if sources != {
            "design_sha256": digest(design_path),
            "template_sha256": digest(template_path),
        }:
            raise RuntimeError("Design or template changed during implementation.")
        return {**result, **sources}
