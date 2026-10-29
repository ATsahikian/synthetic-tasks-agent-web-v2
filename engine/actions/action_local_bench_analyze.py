"""LocalBenchAnalyze action handler."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Self

from engine.json_data import array_value, object_value, text_value
from engine.type_defs import ActionConfig, ExecutionCommand, JsonObject

from ..storage import atomic_json_write
from .base import AgentActionHandler
from .common import digest, read_document


class action_local_bench_analyze(AgentActionHandler):
    name = "local_bench_analyze"
    parameters = frozenset({"agent", "prompt", "source", "source_id"})
    required_parameters = frozenset({"source", "source_id"})

    def run(
        self: Self,
        config: ActionConfig,
        execute: Callable[[ExecutionCommand], int],
        output_path: Path,
    ) -> JsonObject:
        action = config["action"]
        task_path, job_path = Path(config["task_path"]), Path(config["job_path"])
        design_path, template_path = (
            task_path / "design.md",
            task_path.parent / "template.md",
        )
        context = self.context(config)
        benchmark = object_value(action["parameters"]["source"])
        if benchmark.get("design_sha256") != digest(design_path) or benchmark.get(
            "template_sha256"
        ) != digest(template_path):
            raise RuntimeError(
                "Benchmark inputs are stale; rerun implementation and benchmark."
            )
        artifacts = [
            Path(text_value(object_value(row)["result_path"]))
            for row in array_value(benchmark.get("trials", []))
        ] + [
            Path(text_value(file))
            for file in array_value(benchmark.get("trajectories", []))
        ]
        if not benchmark.get("run_completed") or not artifacts:
            raise RuntimeError(
                "A completed local benchmark with saved trial artifacts is required."
            )
        artifact_hashes = {str(file): digest(file) for file in artifacts}
        report = job_path / "analysis.md"
        manifest = {
            "provider": "local_benchmark",
            "benchmark_action_id": action["parameters"]["source_id"],
            "benchmark": benchmark,
            "artifact_sha256": artifact_hashes,
        }
        atomic_json_write(job_path / "analysis.source.json", manifest)
        prompt = context + (
            f"Read {job_path / 'analysis.source.json'} and every listed result and trajectory completely. "
            f"Write only {report}. Do not edit task files, evidence or status. Preserve per-model pass/fail/error/ungraded counts and disclose missing trajectories. "
            "Use >60% as Too easy, 0% as Hard only after excluding setup/verifier failures, and 1–60% as In band. "
            "Recommend only evidence-backed Harden or Simplify changes, tagged with exact names from categories.yaml when available and scores from 1–5. "
            "Include # Trajectory analysis and these sections: Executive summary, Trajectory source, Benchmark calibration, Trial coverage, Cross-trial findings, Difficulty diagnosis, Weak points, Recommendations, Evidence. Cite exact trial paths and trajectory steps."
        )
        self.execute_agent(config, execute, prompt)
        read_document(report)
        if any(
            digest(Path(file)) != hashed for file, hashed in artifact_hashes.items()
        ):
            raise RuntimeError("Benchmark evidence changed during analysis.")
        text = report.read_text("utf-8")
        required = [
            "# Trajectory analysis",
            "Executive summary",
            "Trajectory source",
            "Benchmark calibration",
            "Trial coverage",
            "Cross-trial findings",
            "Difficulty diagnosis",
            "Weak points",
            "Recommendations",
            "Evidence",
        ]
        if any(heading not in text for heading in required) or any(
            str(file) not in text for file in artifacts
        ):
            raise RuntimeError(
                "Analysis report is missing required sections or trial references."
            )
        return {
            "report_path": str(report),
            **{key: benchmark[key] for key in ("design_sha256", "template_sha256")},
        }
