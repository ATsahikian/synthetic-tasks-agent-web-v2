"""Handler for configured executable commands."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable, Self, cast

from engine.type_defs import ActionConfig, ExecutionCommand, ExecutionSpec, JsonObject

from ..errors import EngineError
from .base import ActionHandler
from .common import result_marker
from .definitions import canonical_json


class action_command(ActionHandler):
    name = "command"
    accepts_arbitrary_parameters = True

    runner_fields = frozenset(
        {
            "argv",
            "input",
            "env",
            "cwd",
            "timeout_ms",
            "termination_grace_ms",
            "success_result",
        }
    )

    def configure_runner(self: Self, runner: object, directory: Path) -> ExecutionSpec:
        runner = super().configure_runner(runner, directory)
        if (
            not isinstance(runner.get("argv"), list)
            or not runner["argv"]
            or any(not isinstance(arg, str) or "\x00" in arg for arg in runner["argv"])
            or not runner["argv"][0]
        ):
            raise EngineError("runner.argv must be a nonempty array of strings.")
        for field in ("input", "cwd"):
            if field in runner and not isinstance(runner[field], str):
                raise EngineError(f"runner.{field} must be a string.")
        env = runner.get("env", {})
        if not isinstance(env, dict) or any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
            for key, value in env.items()
        ):
            raise EngineError("runner.env must map valid environment names to strings.")
        for field in ("timeout_ms", "termination_grace_ms"):
            value = runner.get(field)
            if value is not None and (type(value) is not int or value <= 0):
                raise EngineError(f"runner.{field} must be a positive integer or null.")
        if "success_result" in runner and not isinstance(
            runner["success_result"], dict
        ):
            raise EngineError("runner.success_result must be an object.")
        return runner

    def prepare(
        self: Self, runner: ExecutionSpec, parameters: JsonObject
    ) -> ExecutionSpec:
        canonical_json(runner)
        return copy.deepcopy(runner)

    def run(
        self: Self,
        config: ActionConfig,
        execute: Callable[[ExecutionCommand], int],
        output_path: Path,
    ) -> JsonObject:
        # configure_runner validated argv before this execution was captured.
        code = execute(cast(ExecutionCommand, config["execution"]))
        if code != 0:
            raise RuntimeError(f"Command exited with status {code}.")
        result = result_marker(output_path)
        expected = config["execution"].get("success_result", {})
        if any(
            canonical_json(result.get(key)) != canonical_json(value)
            for key, value in expected.items()
        ):
            raise RuntimeError("Command did not emit its required SYNTHETIC_RESULT.")
        return {"exit_code": code, **result}
