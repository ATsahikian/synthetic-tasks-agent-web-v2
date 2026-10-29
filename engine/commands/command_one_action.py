"""A generic debugging command with exactly one caller-supplied action call."""

from __future__ import annotations

from typing import Self

from engine.type_defs import ActionDefinition, ActionStep, CommandDefinition, JsonObject

from ..errors import EngineError
from .base import CommandHandler, action_step


class command_one_action(CommandHandler):
    def validate_definition(self: Self, definition: CommandDefinition) -> None:
        if set(definition) - {"handler", "label", "description"}:
            raise EngineError(
                "one-action cannot define invocation options or bindings."
            )

    def prepare(
        self: Self,
        definition: CommandDefinition,
        actions: dict[str, ActionDefinition],
        parameters: JsonObject,
    ) -> list[ActionStep]:
        if "action" not in parameters or set(parameters) - {
            "action",
            "action_parameters",
        }:
            raise EngineError(
                "one-action requires action and optionally action_parameters."
            )
        return [
            action_step(
                actions, parameters["action"], parameters.get("action_parameters", {})
            )
        ]
