"""Implementation action handler."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Self

from engine.type_defs import ActionConfig, ExecutionCommand, ExecutionSpec, JsonObject

from ..git_worker.transactions import file_manifest
from .base import AgentActionHandler
from .common import (
    check_guidance,
    design_sources,
    digest,
    implementation_digest,
    require_source,
    result_marker,
    task_guidance,
)


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
        spec["task_guide_default"] = (
            Path(__file__).resolve().parent.parent / "skills/impl-guidance.md"
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
        guide_path, guide = task_guidance(
            task_path, "impl-guidance.md", spec.get("task_guide_default", "")
        )
        writeup = file_manifest(task_path, ["writeup.md"])
        context = self.context(config)
        if parameters.get("reference_task"):
            context += (
                f"Explicit reference task (read only): {parameters['reference_task']}\n"
            )
        prompt = (
            context
            + "\n"
            + spec["skill"]
            + (
                f"\nImplementation guidance source: {guide_path or 'bundled generic implementation guide'}.\n"
                f"Resolve local guide example paths relative to {task_path.parent}.\n"
                f"Read and follow this entire guide before editing. It is read-only.\n"
                f"<impl-guidance>\n{guide}\n</impl-guidance>\n"
                "Do not read writeup-guidance.md or author writeup.md during implementation.\n"
            )
        )
        self.execute_agent(config, execute, prompt)
        check_guidance(guide_path, guide)
        if file_manifest(task_path, ["writeup.md"]) != writeup:
            raise RuntimeError("Implementation must not change writeup.md.")
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
        return {
            **result,
            **sources,
            "implementation_sha256": implementation_digest(task_path),
        }
