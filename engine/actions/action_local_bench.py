"""LocalBench action handler."""

import json
import os
from pathlib import Path

from .base import AsyncActionHandler
from .common import require_source


class action_local_bench(AsyncActionHandler):
    name = "local_bench"
    parameters = frozenset({
        "source", "model", "benchmark_agent", "attempts", "concurrency",
        "timeout_multiplier", "agent_timeout_multiplier",
    })

    required_parameters = frozenset({"source"})

    def prepare(self, runner, parameters):
        spec = super().prepare(runner, parameters)
        spec["codimango_bin"] = os.environ.get("CODIMANGO_BIN", "codimango")
        return spec

    def run(self, config, execute, output_path):
        action, spec = config["action"], config["execution"]
        parameters = action["parameters"]
        task_path = Path(config["task_path"])
        approval = require_source(config, approved=True, oracle=True)
        attempts = parameters.get("attempts", 5)
        run_path = Path(config["job_path"]) / "benchmark"
        argv = [
            spec["codimango_bin"],
            "bench",
            "run",
            "-p",
            action["task"],
            "-a",
            parameters.get("benchmark_agent", "metacode"),
            "-m",
            parameters.get("model", "meta/avocado-code-flex"),
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
        code = execute(
            {"argv": argv, "cwd": str(task_path.parent), "timeout_ms": None, "literal": True}
        )
        result = benchmark_summary(run_path, attempts)
        result.update(
            {
                "runner_exit_code": code,
                "design_sha256": approval["design_sha256"],
                "template_sha256": approval["template_sha256"],
                "model": parameters.get("model", "meta/avocado-code-flex"),
            }
        )
        if not result["run_completed"]:
            raise RuntimeError(
                f"Benchmark incomplete: {result['completed']}/{attempts} trials finished (exit {code})."
            )
        return result


def trial_artifacts(directory):
    results, trajectories = [], []
    for file in sorted(directory.rglob("*.json")):
        if file.is_symlink():
            continue
        if file.name == "result.json":
            results.append(file)
        elif file.name in {"trajectory.json", "trajectory.atif.json", "atif.json"}:
            trajectories.append(file)
    return results, trajectories


def benchmark_summary(directory, attempts):
    results, trajectories = trial_artifacts(directory)
    trials = []
    for file in results:
        value = json.loads(file.read_text("utf-8"))
        if not isinstance(value, dict) or not any(
            key in value for key in ("verifier_result", "exception_info", "trial_name")
        ):
            continue
        rewards = (value.get("verifier_result") or {}).get("rewards") or {}
        reward = rewards.get("reward", rewards.get("resolved"))
        error = value.get("exception_info")
        trials.append({"result_path": str(file), "reward": reward, "error": error})
    completed = sum(row["reward"] is not None or bool(row["error"]) for row in trials)
    return {
        "total": len(trials),
        "completed": completed,
        "passed": sum(type(row["reward"]) in (int, float) and row["reward"] > 0 for row in trials),
        "errored": sum(bool(row["error"]) for row in trials),
        "run_completed": len(trials) == attempts and completed == attempts,
        "trials": trials,
        "trajectories": [str(file) for file in trajectories],
        "result_path": str(directory),
    }
