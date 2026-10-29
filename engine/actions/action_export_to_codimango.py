"""Configure and validate the Codimango export Git action."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Self

from engine.json_data import object_value
from engine.type_defs import ActionConfig, ExecutionCommand, ExecutionSpec, JsonObject

from ..errors import EngineError
from ..git_worker.export import export_task
from .base import GitActionHandler
from .common import digest


class action_export_to_codimango(GitActionHandler):
    name = "export_to_codimango"
    parameters = frozenset({"source"})
    required_parameters = frozenset({"source"})
    runner_fields = frozenset({"repo_path"})

    def configure_runner(self: Self, runner: object, directory: Path) -> ExecutionSpec:
        runner = super().configure_runner(runner, directory)
        repo_path = runner.get("repo_path")
        if (
            not isinstance(repo_path, str)
            or not repo_path.strip()
            or "\x00" in repo_path
        ):
            raise EngineError(
                "export_to_codimango runner.repo_path must be nonempty text."
            )
        runner["repo_path"] = str((directory / Path(repo_path).expanduser()).resolve())
        return runner

    def prepare(
        self: Self, runner: ExecutionSpec, parameters: JsonObject
    ) -> ExecutionSpec:
        spec = super().prepare(runner, parameters)
        spec["repo_path"] = str(
            Path(os.environ.get("CODIMANGO_REPO_ROOT") or spec["repo_path"])
            .expanduser()
            .resolve()
        )
        return spec

    def run(
        self: Self,
        config: ActionConfig,
        execute: Callable[[ExecutionCommand], int],
        output_path: Path,
    ) -> JsonObject:
        spec = config["execution"]
        task_path = Path(config["task_path"])
        design_path, template_path = (
            task_path / "design.md",
            task_path.parent / "template.md",
        )
        inputs = object_value(config["action"]["parameters"]["source"])
        if inputs.get("oracle_passed") is not True:
            raise RuntimeError(
                "A verified passing implementation oracle is required before exporting."
            )
        if inputs.get("design_sha256") != digest(design_path) or inputs.get(
            "template_sha256"
        ) != digest(template_path):
            raise RuntimeError(
                "Task inputs changed; rerun the prerequisite before exporting."
            )
        return export_task(
            task_path,
            Path(spec["repo_path"]),
            execute,
            job_path=Path(config["job_path"]),
            action_id=config["action"]["action_id"],
        )
