from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Never, cast

from engine.type_defs import ActionDefinition, PathLike

from ..errors import EngineError

NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
DEFINITION_FIELDS = {
    "label",
    "description",
    "execution_type",
    "handler",
    "runner",
}


def reject_constant(value: str) -> Never:
    raise ValueError(f"Non-finite JSON number: {value}")


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def fingerprint(operation: str, body: object) -> str:
    encoded = canonical_json({"operation": operation, "body": body}).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_name(value: object) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise EngineError(
            "Action names must start with a letter and use letters, digits, _ or -."
        )
    return value


def validate_request_id(value: object) -> str:
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise EngineError("request_id must be 1–128 letters, digits, _, ., : or -.")
    return value


def load_actions(file: PathLike) -> dict[str, ActionDefinition]:
    from .registry import handler_for

    try:
        config = json.loads(
            Path(file).read_text("utf-8"), parse_constant=reject_constant
        )
    except (OSError, ValueError) as error:
        raise EngineError(f"Cannot load action definitions: {error}") from error
    if (
        not isinstance(config, dict)
        or type(config.get("schema_version")) is not int
        or config.get("schema_version") != 1
        or not isinstance(config.get("actions"), dict)
        or set(config) != {"schema_version", "actions"}
    ):
        raise EngineError(
            "Action configuration needs schema_version: 1 and an actions object."
        )
    actions: dict[str, ActionDefinition] = {}
    for name, definition in config["actions"].items():
        validate_name(name)
        if (
            not isinstance(definition, dict)
            or "handler" not in definition
            or set(definition) - DEFINITION_FIELDS
        ):
            raise EngineError(
                f"Action {name} needs an action class handler and no workflow dependencies."
            )
        for field in ("label", "description"):
            if field in definition and not isinstance(definition[field], str):
                raise EngineError(f"Action {name} {field} must be text.")
        if definition.get("execution_type", "script") not in ("agent", "script"):
            raise EngineError("execution_type must be agent or script.")
        handler = handler_for(definition["handler"])
        runner = handler.configure_runner(
            definition.get("runner", {}), Path(file).resolve().parent
        )
        actions[name] = copy.deepcopy(cast(ActionDefinition, definition))
        if "runner" in definition:
            actions[name]["runner"] = runner

    return actions
