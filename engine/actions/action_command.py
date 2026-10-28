"""Handler for configured executable commands."""

import json

from .base import AsyncActionHandler
from .common import result_marker
from .definitions import canonical_json
from ..errors import EngineError


class action_command(AsyncActionHandler):
    name = "command"

    runner_fields = frozenset({
        "argv", "input", "env", "cwd", "timeout_ms",
        "termination_grace_ms", "success_result",
    })

    def configure_runner(self, runner, directory):
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
        if "success_result" in runner and not isinstance(runner["success_result"], dict):
            raise EngineError("runner.success_result must be an object.")
        return runner

    def prepare(self, runner, parameters):
        return json.loads(canonical_json(runner))

    def run(self, config, execute, output_path):
        code = execute(config["execution"])
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
