import hashlib
import json
import re
from pathlib import Path

from .errors import EngineError


NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


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
        or config.get("schema_version") != 1
        or not isinstance(config.get("actions"), dict)
        or set(config) != {"schema_version", "actions"}
    ):
        raise EngineError("Action configuration needs schema_version: 1 and an actions object.")
    actions = {}
    for name, definition in config["actions"].items():
        validate_name(name)
        if not isinstance(definition, dict) or set(definition) != {"dependencies"}:
            raise EngineError(f"Action {name} needs a dependencies array.")
        dependencies = definition["dependencies"]
        if not isinstance(dependencies, list):
            raise EngineError(f"Action {name} needs a dependencies array.")
        for dependency in dependencies:
            validate_name(dependency)
        if len(set(dependencies)) != len(dependencies):
            raise EngineError(f"Action {name} repeats a dependency.")
        actions[name] = {"dependencies": list(dependencies)}

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
