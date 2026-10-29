"""Redesign action handler."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Self

from engine.type_defs import ActionConfig, ExecutionCommand, JsonObject

from ..storage import sync_directory
from .base import AgentActionHandler
from .common import design_sources, digest, read_document


class action_redesign(AgentActionHandler):
    name = "redesign"
    parameters = frozenset({"agent", "prompt"})

    def run(
        self: Self,
        config: ActionConfig,
        execute: Callable[[ExecutionCommand], int],
        output_path: Path,
    ) -> JsonObject:
        task_path, job_path = Path(config["task_path"]), Path(config["job_path"])
        sources = design_sources(task_path)
        design_path = task_path / "design.md"
        template_path = task_path.parent / "template.md"
        context = self.context(config)
        candidate = job_path / "design.candidate.md"
        prompt = context + (
            f"Read design.md, the parent template.md, categories.yaml when present, and existing design/template Git history. "
            f"Revise the design against the template and original user request. Write only {candidate}. "
            "Preserve the task name and useful design details. Do not modify published design.md, status files, implementation, benchmark artifacts or the template. Do not scaffold or implement."
        )
        self.execute_agent(config, execute, prompt)
        read_document(candidate)
        if sources != {
            "design_sha256": digest(design_path),
            "template_sha256": digest(template_path),
        }:
            raise RuntimeError(
                "Design or template changed during redesign; candidate was not published."
            )
        temporary = job_path / "design.publish.md"
        with temporary.open("wb") as stream:
            stream.write(candidate.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, design_path)
        sync_directory(task_path)
        return {
            "design_sha256": digest(design_path),
            "template_sha256": sources["template_sha256"],
            "candidate_path": str(candidate),
        }
