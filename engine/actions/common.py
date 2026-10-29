"""Shared validation, source capture, and agent protocol helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import cast

from engine.json_data import loads, object_value
from engine.type_defs import (
    ActionConfig,
    ExecutionCommand,
    ExecutionSpec,
    JsonObject,
    JsonValue,
    PathLike,
)

from ..errors import EngineError
from .definitions import reject_constant


def validate_parameters(
    name: str, parameters: JsonObject, allowed: frozenset[str], required: frozenset[str]
) -> None:
    if set(parameters) - allowed:
        raise EngineError(
            f"Unknown {name} parameters: {', '.join(sorted(set(parameters) - allowed))}."
        )
    if "source" in parameters and not isinstance(parameters["source"], dict):
        raise EngineError(
            "source must be an object containing the action's source data."
        )
    if "source_id" in parameters and not isinstance(parameters["source_id"], str):
        raise EngineError("source_id must be text.")
    for key in (
        "prompt",
        "reference_task",
        "evidence_path",
        "model",
        "benchmark_agent",
    ):
        value = parameters.get(key)
        if key in parameters and (
            not isinstance(value, str) or not value.strip() or "\x00" in value
        ):
            raise EngineError(f"{key} must be nonempty text.")
    for key in required:
        if key not in parameters:
            raise EngineError(f"{name} requires {key}.")
    for key in ("attempts", "concurrency"):
        value = parameters.get(key)
        if key in parameters and (type(value) is not int or not 1 <= value <= 100):
            raise EngineError(f"{key} must be an integer from 1 to 100.")
    for key in ("timeout_multiplier", "agent_timeout_multiplier"):
        value = parameters.get(key)
        if key in parameters and (
            not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
        ):
            raise EngineError(f"{key} must be a positive number.")


def design_sources(task_path: Path) -> dict[str, str]:
    design_path, template_path = (
        task_path / "design.md",
        task_path.parent / "template.md",
    )
    read_document(design_path)
    read_document(template_path)
    return {
        "design_sha256": digest(design_path),
        "template_sha256": digest(template_path),
    }


def require_source(
    config: ActionConfig, *, approved: bool = False, oracle: bool = False
) -> JsonObject:
    approval = object_value(config["action"]["parameters"]["source"])
    task_path = Path(config["task_path"])
    if approval.get("design_sha256") != digest(task_path / "design.md") or approval.get(
        "template_sha256"
    ) != digest(task_path.parent / "template.md"):
        raise RuntimeError(
            "Design or template changed; approve the current design and rerun implementation."
        )
    if approved and approval.get("approved") is not True:
        raise RuntimeError("Approved source data is required.")
    if oracle and approval.get("oracle_passed") is not True:
        raise RuntimeError("A verified passing implementation oracle is required.")
    return approval


def digest(file: PathLike) -> str:
    return hashlib.sha256(Path(file).read_bytes()).hexdigest()


def read_document(file: PathLike) -> str:
    path = Path(file)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Required document must be a regular file: {file}")
    value = path.read_text("utf-8")
    if not value.strip():
        raise RuntimeError(f"Required document is empty: {file}")
    return value


def agent_command(
    spec: ExecutionSpec, task_path: Path, prompt: str
) -> ExecutionCommand:
    agent, binary = spec["agent"], spec["agent_bin"]
    if agent == "codex":
        return {
            "argv": [
                binary,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--approve-for-me",
                "--color",
                "never",
                "-C",
                str(task_path),
                "-",
            ],
            "input": prompt,
        }
    if agent == "claude":
        return {
            "argv": [
                binary,
                "--model",
                "claude-opus-4-8",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--no-session-persistence",
                "--safe-mode",
                "--restricted",
                "--permission-mode",
                "auto",
                "--permission-prompts",
                "none",
                "--tools",
                "Read,Write,Edit,Glob,Grep,Bash",
                "--add-dir",
                str(task_path.parent),
                "--add-dir",
                spec["templates_path"],
            ],
            "input": prompt,
            "env": {"CLAUDE_CODE_DISABLE_1M_CONTEXT": "1"},
        }
    return {
        "argv": [
            binary,
            "exec",
            "--provider",
            "meta",
            "--workspace",
            str(task_path),
            "--approval-mode",
            "never",
            "--no-session-log",
            "--disable-request-user-input",
            "--trust-workspace",
            prompt,
        ]
    }


def result_marker(output_path: Path) -> JsonObject:
    # Parse normal output and Claude's JSON event stream. A marker in a tool call
    # or echoed prompt must not count as the agent's final result.
    marker: JsonObject | None = None
    with output_path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            texts: list[str] = [line]
            if line.startswith("{"):
                try:
                    event = loads(line)
                    if isinstance(event, dict) and event.get("type") == "result":
                        event_text = event.get("result", "")
                        texts = [event_text] if isinstance(event_text, str) else []
                    else:
                        texts = []
                except (ValueError, AttributeError):
                    texts = []
            for text in texts:
                for match in re.finditer(
                    r"^SYNTHETIC_RESULT=(\{[^\n]*\})\s*$", text, re.MULTILINE
                ):
                    try:
                        value = cast(
                            JsonValue,
                            json.loads(match[1], parse_constant=reject_constant),
                        )
                        if isinstance(value, dict):
                            marker = value
                    except ValueError:
                        pass
    return marker or {}
