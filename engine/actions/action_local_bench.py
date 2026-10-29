"""LocalBench action handler."""

from __future__ import annotations

import os
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, Self

from engine.json_data import loads, object_value, text_value
from engine.type_defs import (
    ActionConfig,
    ExecutionCommand,
    ExecutionSpec,
    JsonObject,
    JsonValue,
)

from .base import ActionHandler
from .common import require_source
from .metacode_cache import metacode_local_cache


class action_local_bench(ActionHandler):
    name = "local_bench"
    parameters = frozenset(
        {
            "source",
            "model",
            "benchmark_agent",
            "attempts",
            "concurrency",
            "timeout_multiplier",
            "agent_timeout_multiplier",
        }
    )

    required_parameters = frozenset({"source"})

    def prepare(
        self: Self, runner: ExecutionSpec, parameters: JsonObject
    ) -> ExecutionSpec:
        spec = super().prepare(runner, parameters)
        spec["codimango_bin"] = os.environ.get("CODIMANGO_BIN", "codimango")
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
        approval = require_source(config, approved=True, oracle=True)
        benchmark_agent = text_value(parameters.get("benchmark_agent", "metacode"))
        attempts = parameters.get("attempts", 5)
        assert isinstance(attempts, int)
        run_path = Path(config["job_path"]) / "benchmark"
        argv = [
            spec["codimango_bin"],
            "bench",
            "run",
            "-p",
            action["task"],
            "-a",
            benchmark_agent,
            "-m",
            text_value(parameters.get("model", "meta/avocado-code-flex")),
            "-k",
            str(attempts),
            "-n",
            str(parameters.get("concurrency", attempts)),
            "--ordinary",
            "--timeout-multiplier",
            str(parameters.get("timeout_multiplier", 1.5)),
            "--agent-timeout-multiplier",
            str(parameters.get("agent_timeout_multiplier", 54)),
            "-o",
            str(run_path),
        ]
        cache_context = (
            metacode_local_cache() if benchmark_agent == "metacode" else nullcontext()
        )
        with cache_context as cache:
            if cache is not None:
                with output_path.open("a", encoding="utf-8") as output:
                    output.write(f"[benchmark] Using flat MetaCode local cache: {cache}\n")
            code = execute(
                {
                    "argv": argv,
                    "cwd": str(task_path.parent),
                    "env": (
                        {"HARBOR_METACODE_LOCAL_CACHE": str(cache)}
                        if cache is not None
                        else {}
                    ),
                    "timeout_ms": None,
                    "literal": True,
                }
            )
        result = benchmark_summary(run_path, attempts)
        result.update(
            {
                "runner_exit_code": code,
                "design_sha256": approval["design_sha256"],
                "template_sha256": approval["template_sha256"],
                "model": text_value(parameters.get("model", "meta/avocado-code-flex")),
            }
        )
        if not result["run_completed"]:
            raise RuntimeError(
                f"Benchmark incomplete: {result['completed']}/{attempts} trials finished (exit {code})."
            )
        return result


def trial_artifacts(directory: Path) -> tuple[list[Path], list[Path]]:
    results, trajectories = [], []
    for file in sorted(directory.rglob("*.json")):
        if file.is_symlink():
            continue
        if file.name == "result.json":
            results.append(file)
        elif file.name in {"trajectory.json", "trajectory.atif.json", "atif.json"}:
            trajectories.append(file)
    return results, trajectories


def benchmark_summary(directory: Path, attempts: int) -> JsonObject:
    results, trajectories = trial_artifacts(directory)
    trials: list[JsonValue] = []
    for file in results:
        value = loads(file.read_text("utf-8"))
        if not isinstance(value, dict) or not any(
            key in value for key in ("verifier_result", "exception_info", "trial_name")
        ):
            continue
        rewards = object_value(
            object_value(value.get("verifier_result") or {}).get("rewards") or {}
        )
        reward = rewards.get("reward", rewards.get("resolved"))
        error = value.get("exception_info")
        trials.append({"result_path": str(file), "reward": reward, "error": error})
    completed = sum(
        object_value(row)["reward"] is not None or bool(object_value(row)["error"])
        for row in trials
    )
    return {
        "total": len(trials),
        "completed": completed,
        "passed": sum(
            isinstance(reward, (int, float))
            and not isinstance(reward, bool)
            and reward > 0
            for reward in (object_value(row)["reward"] for row in trials)
        ),
        "errored": sum(bool(object_value(row)["error"]) for row in trials),
        "run_completed": len(trials) == attempts and completed == attempts,
        "trials": trials,
        "trajectories": [str(file) for file in trajectories],
        "result_path": str(directory),
    }
