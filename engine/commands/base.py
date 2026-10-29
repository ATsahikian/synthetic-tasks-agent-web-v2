"""Prepare immutable action calls without executing any work."""

import copy
import uuid
from abc import ABC, abstractmethod

from ..actions import prepare_execution, validate_name
from ..actions.registry import handler_for
from ..errors import EngineError


def action_step(actions, name, parameters, dependencies=(), bindings=None, ids=None):
    validate_name(name)
    definition = actions.get(name)
    if definition is None:
        raise EngineError("Unknown predefined action.", status=404, code="action_not_found")
    if not isinstance(parameters, dict):
        raise EngineError("action_parameters must be an object.")
    bindings = copy.deepcopy(bindings or {})
    prepared_parameters = copy.deepcopy(parameters)
    handler = handler_for(definition["handler"])
    for parameter, binding in bindings.items():
        if parameter not in handler.parameters and not handler.accepts_arbitrary_parameters:
            raise EngineError(f"Unknown bound parameter {parameter} for {name}.")
        # Result values become available only after the producer finishes. Preparation
        # validates their object contract; the action validates their contents at runtime.
        prepared_parameters[parameter] = (
            ids[binding["step"]] if binding["value"] == "action_id" else {}
        )
    execution = prepare_execution(definition, prepared_parameters)
    return {
        "action_id": ids[name] if ids else str(uuid.uuid4()),
        "action": name,
        "parameters": copy.deepcopy(parameters),
        "dependencies": list(dependencies),
        "parameter_bindings": bindings,
        "execution": execution,
        "mode": definition["mode"],
        "execution_type": definition.get("execution_type", "script"),
    }


class CommandHandler(ABC):
    @abstractmethod
    def validate_definition(self, definition):
        """Validate the command's structure independently of available actions."""

    @abstractmethod
    def prepare(self, definition, actions, parameters):
        """Return frozen, ordered action calls; never read task history."""
