"""Configure and validate the Codimango export Git action."""

import os
from pathlib import Path

from .base import GitActionHandler
from .common import digest
from ..errors import EngineError
from ..git_worker.export import export_task


class action_export_to_codimango(GitActionHandler):
    name = "export_to_codimango"
    parameters = frozenset({"source"})
    required_parameters = frozenset({"source"})
    runner_fields = frozenset({"repo_path"})

    def configure_runner(self, runner, directory):
        runner = super().configure_runner(runner, directory)
        repo_path = runner.get("repo_path")
        if not isinstance(repo_path, str) or not repo_path.strip() or "\x00" in repo_path:
            raise EngineError("export_to_codimango runner.repo_path must be nonempty text.")
        runner["repo_path"] = str((directory / Path(repo_path).expanduser()).resolve())
        return runner

    def prepare(self, runner, parameters):
        spec = super().prepare(runner, parameters)
        spec["repo_path"] = str(Path(os.environ.get("CODIMANGO_REPO_ROOT") or spec["repo_path"]).expanduser().resolve())
        return spec

    def run(self, config, execute, output_path):
        spec = config["execution"]
        task_path = Path(config["task_path"])
        design_path, template_path = task_path / "design.md", task_path.parent / "template.md"
        inputs = config["action"]["parameters"]["source"]
        if inputs.get("oracle_passed") is not True:
            raise RuntimeError("A verified passing implementation oracle is required before exporting.")
        if inputs.get("design_sha256") != digest(design_path) or inputs.get(
            "template_sha256"
        ) != digest(template_path):
            raise RuntimeError("Task inputs changed; rerun the prerequisite before exporting.")
        return export_task(task_path, Path(spec["repo_path"]), execute,
                           job_path=Path(config["job_path"]), action_id=config["action"]["action_id"])
