"""Load the bundled command catalog; runtime arguments arrive through requests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import cast

from engine.type_defs import CommandDefinition

from ..actions import reject_constant, validate_name
from ..errors import EngineError
from .registry import handler_for


def load_commands(
    definitions: dict[str, CommandDefinition] | None = None,
) -> dict[str, CommandDefinition]:
    if definitions is None:
        try:
            config = json.loads(
                (Path(__file__).parent / "command.json").read_text("utf-8"),
                parse_constant=reject_constant,
            )
        except (OSError, ValueError) as error:
            raise EngineError(f"Cannot load command definitions: {error}") from error
    else:
        config = {"schema_version": 1, "commands": copy.deepcopy(definitions)}
    if (
        not isinstance(config, dict)
        or set(config) != {"schema_version", "commands"}
        or type(config["schema_version"]) is not int
        or config["schema_version"] != 1
        or not isinstance(config["commands"], dict)
    ):
        raise EngineError(
            "Command configuration needs schema_version: 1 and a commands object."
        )
    for name, definition in config["commands"].items():
        validate_name(name)
        if not isinstance(definition, dict) or "handler" not in definition:
            raise EngineError("Each command requires a handler.")
        for field in ("label", "description"):
            if field in definition and not isinstance(definition[field], str):
                raise EngineError(f"Command {field} must be text.")
        handler_for(definition["handler"]).validate_definition(
            cast(CommandDefinition, definition)
        )
    return cast(dict[str, CommandDefinition], config["commands"])
