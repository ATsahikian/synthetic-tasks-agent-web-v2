"""Prepare immutable action calls without executing any work."""

from __future__ import annotations

import copy
import uuid
from abc import ABC, abstractmethod
from typing import Self, Sequence, cast

from engine.type_defs import (
    ActionDefinition,
    ActionStep,
    Binding,
    CommandDefinition,
    JsonObject,
)

from ..actions import prepare_execution, validate_name
from ..actions.registry import handler_for
from ..errors import EngineError


def action_step(
    actions: dict[str, ActionDefinition],
    name: object,
    parameters: object,
    dependencies: Sequence[str] = (),
    bindings: dict[str, Binding] | None = None,
    ids: dict[str, str] | None = None,
) -> ActionStep:
    name = validate_name(name)
    definition = actions.get(name)
    if definition is None:
        raise EngineError(
            "Unknown predefined action.", status=404, code="action_not_found"
        )
    if not isinstance(parameters, dict):
        raise EngineError("action_parameters must be an object.")
    bindings = copy.deepcopy(bindings or {})
    parameters = cast(JsonObject, parameters)
    prepared_parameters = copy.deepcopy(parameters)
    handler = handler_for(definition["handler"])
    for parameter, binding in bindings.items():
        if (
            parameter not in handler.parameters
            and not handler.accepts_arbitrary_parameters
        ):
            raise EngineError(f"Unknown bound parameter {parameter} for {name}.")
        # Result values become available only after the producer finishes. Preparation
        # validates their object contract; the action validates their contents at runtime.
        if binding["value"] == "action_id":
            assert ids is not None
        prepared_parameters[parameter] = (
            ids[binding["step"]]
            if ids is not None and binding["value"] == "action_id"
            else {}
        )
    execution = prepare_execution(definition, prepared_parameters)
    return {
        "action_id": ids[name] if ids else str(uuid.uuid4()),
        "action": name,
        "parameters": copy.deepcopy(parameters),
        "dependencies": list(dependencies),
        "parameter_bindings": bindings,
        "execution": execution,
        "execution_type": definition.get("execution_type", "script"),
    }


class CommandHandler(ABC):
    @abstractmethod
    def validate_definition(self: Self, definition: CommandDefinition) -> None:
        """Validate the command's structure independently of available actions."""

    @abstractmethod
    def prepare(
        self: Self,
        definition: CommandDefinition,
        actions: dict[str, ActionDefinition],
        parameters: JsonObject,
    ) -> list[ActionStep]:
        """Return frozen, ordered action calls; never read task history."""
