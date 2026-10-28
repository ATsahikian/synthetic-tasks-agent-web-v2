import copy
import hashlib
import json
import re
from pathlib import Path

from .errors import EngineError


NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
DEFINITION_FIELDS = {
    "dependencies",
    "label",
    "description",
    "execution_type",
    "runner",
}


def validate_runner(runner):
    if not isinstance(runner, dict) or not runner:
        raise EngineError("runner must be a nonempty object.")
    if "builtin" in runner:
        fields = {"builtin"}
        if runner["builtin"] == "export_to_codimango":
            fields.add("repo_path")
        if (
            set(runner) != fields
            or not isinstance(runner["builtin"], str)
            or runner["builtin"]
            not in {
                "commit-desing",
                # Keep captured pre-rename runner definitions executable.
                "approve_design",
                "benchmark",
                "analyze",
                "redesign",
                "implement",
                "commit-impl",
                "local_bench",
                "local_bench_analyze",
                "export_to_codimango",
                "custom",
                "troubleshoot",
            }
        ):
            raise EngineError("Unknown builtin runner.")
        if runner["builtin"] == "export_to_codimango" and (
            not isinstance(runner["repo_path"], str)
            or not runner["repo_path"].strip()
            or "\x00" in runner["repo_path"]
        ):
            raise EngineError("export_to_codimango runner.repo_path must be nonempty text.")
        return
    if set(runner) - {
        "argv",
        "input",
        "env",
        "cwd",
        "timeout_ms",
        "termination_grace_ms",
        "success_result",
    }:
        raise EngineError("Unknown runner fields.")
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


def reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def fingerprint(operation, body):
    encoded = canonical_json({"operation": operation, "body": body}).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise EngineError("Action names must start with a letter and use letters, digits, _ or -.")
    return value


def validate_request_id(value):
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise EngineError("request_id must be 1–128 letters, digits, _, ., : or -.")
    return value


def load_actions(file):
    try:
        config = json.loads(Path(file).read_text("utf-8"), parse_constant=reject_constant)
    except (OSError, ValueError) as error:
        raise EngineError(f"Cannot load action definitions: {error}") from error
    if (
        not isinstance(config, dict)
        or type(config.get("schema_version")) is not int
        or config.get("schema_version") != 1
        or not isinstance(config.get("actions"), dict)
        or set(config) != {"schema_version", "actions"}
    ):
        raise EngineError("Action configuration needs schema_version: 1 and an actions object.")
    actions = {}
    for name, definition in config["actions"].items():
        validate_name(name)
        if (
            not isinstance(definition, dict)
            or "dependencies" not in definition
            or set(definition) - DEFINITION_FIELDS
        ):
            raise EngineError(f"Action {name} needs a dependencies array.")
        dependencies = definition["dependencies"]
        if not isinstance(dependencies, list):
            raise EngineError(f"Action {name} needs a dependencies array.")
        for dependency in dependencies:
            validate_name(dependency)
        if len(set(dependencies)) != len(dependencies):
            raise EngineError(f"Action {name} repeats a dependency.")
        for field in ("label", "description"):
            if field in definition and not isinstance(definition[field], str):
                raise EngineError(f"Action {name} {field} must be text.")
        if definition.get("execution_type", "script") not in ("agent", "script"):
            raise EngineError("execution_type must be agent or script.")
        if "runner" in definition:
            validate_runner(definition["runner"])
        actions[name] = copy.deepcopy(definition)
        if definition.get("runner", {}).get("builtin") == "export_to_codimango":
            repo_path = Path(definition["runner"]["repo_path"]).expanduser()
            actions[name]["runner"]["repo_path"] = str((Path(file).resolve().parent / repo_path).resolve())

    visiting, visited = set(), set()

    def visit(name):
        if name in visiting:
            raise EngineError(f"Action dependencies contain a cycle at {name}.")
        if name in visited:
            return
        visiting.add(name)
        for dependency in actions[name]["dependencies"]:
            if dependency not in actions:
                raise EngineError(f"Action {name} has unknown dependency {dependency}.")
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in actions:
        visit(name)
    return actions
