"""Generate a researcher writeup through a fresh first-party Muse invocation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Self

from engine.type_defs import ActionConfig, ExecutionCommand, ExecutionSpec, JsonObject

from .base import AgentActionHandler
from .common import (
    check_guidance,
    digest,
    read_document,
    require_implementation,
    result_marker,
    task_guidance,
)


class action_write_up(AgentActionHandler):
    name = "write_up"
    parameters = frozenset({"source", "prompt"})
    required_parameters = frozenset({"source"})

    def prepare(
        self: Self, runner: ExecutionSpec, parameters: JsonObject
    ) -> ExecutionSpec:
        spec = super().prepare(runner, parameters)
        spec["agent"] = "muse"
        spec["agent_bin"] = spec["agent_binaries"]["muse"]
        spec["task_guide_default"] = (
            Path(__file__).resolve().parent.parent / "skills/writeup-guidance.md"
        ).read_text("utf-8")
        return spec

    def run(
        self: Self,
        config: ActionConfig,
        execute: Callable[[ExecutionCommand], int],
        output_path: Path,
    ) -> JsonObject:
        if config["execution"]["agent"] != "muse":
            raise RuntimeError("write_up requires first-party Muse with provider meta.")
        task = Path(config["task_path"])
        source = require_implementation(config)
        guide_path, guide = task_guidance(
            task, "writeup-guidance.md", config["execution"]["task_guide_default"]
        )
        writeup = task / "writeup.md"
        if writeup.is_symlink() or (writeup.exists() and not writeup.is_file()):
            raise RuntimeError("writeup.md must be a regular file.")
        prompt = self.context(config) + (
            f"\nWrite only {writeup}. Read task.toml, README.md, instruction.md, design.md, "
            "the approved parent template.md, build/ and environment/ sources, solution/, and tests/ "
            "as needed to establish task facts. Ground every claim in these sources.\n"
            f"Writeup guidance source: {guide_path or 'bundled generic writeup guide'}.\n"
            f"Resolve local guide example paths relative to {task.parent}.\n"
            f"Before generating, read and follow this entire read-only guide:\n"
            f"<writeup-guidance>\n{guide}\n</writeup-guidance>\n"
            "Do not read impl-guidance.md. Changes are allowed only to writeup.md. "
            "Do not run builds, repair the oracle, or edit implementation sources. "
            "Include a notice in writeup.md that human review is needed. "
            "Report the line count and section list when finished, then emit the standalone line "
            'SYNTHETIC_RESULT={"writeup_generated":true}\n'
        )
        self.execute_agent(config, execute, prompt)
        require_implementation(config)
        check_guidance(guide_path, guide)
        read_document(writeup)
        if result_marker(output_path).get("writeup_generated") is not True:
            raise RuntimeError(
                "Writeup generation did not emit a successful completion result."
            )
        return {**source, "writeup_generated": True, "writeup_sha256": digest(writeup)}
