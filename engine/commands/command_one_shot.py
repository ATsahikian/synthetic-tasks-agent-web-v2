"""Execute the ordered calls and parameter bindings in a command definition."""

import copy
import uuid

from .base import CommandHandler, action_step
from ..actions import validate_name
from ..actions.registry import handler_for
from ..errors import EngineError


class command_one_shot(CommandHandler):
    def validate_definition(self, definition):
        if set(definition) - {"handler", "label", "description", "actions"}:
            raise EngineError("Unknown chain command fields.")
        steps = definition.get("actions")
        if not isinstance(steps, list) or not steps:
            raise EngineError("A chain command requires a nonempty actions array.")
        seen = set()
        for step in steps:
            if not isinstance(step, dict) or set(step) != {"action", "dependencies", "parameter_bindings"}:
                raise EngineError("Each command step needs action, dependencies, and parameter_bindings.")
            name = validate_name(step["action"])
            if name in seen:
                raise EngineError("A command cannot repeat an action.")
            dependencies = step["dependencies"]
            if not isinstance(dependencies, list):
                raise EngineError("Command dependencies must be an array.")
            for dependency in dependencies:
                validate_name(dependency)
            if len(set(dependencies)) != len(dependencies) or not set(dependencies) <= seen:
                raise EngineError("Command dependencies must name earlier steps; cycles are invalid.")
            bindings = step["parameter_bindings"]
            if not isinstance(bindings, dict):
                raise EngineError("parameter_bindings must be an object.")
            for parameter, binding in bindings.items():
                if not isinstance(parameter, str) or not parameter:
                    raise EngineError("Bound parameter names must be nonempty text.")
                if (
                    not isinstance(binding, dict)
                    or set(binding) != {"step", "value"}
                    or binding["step"] not in dependencies
                    or binding["value"] not in ("result", "action_id")
                ):
                    raise EngineError("Parameter bindings must select a dependency's result or action_id.")
            seen.add(name)

    def prepare(self, definition, actions, parameters):
        overrides = parameters.get("action_parameters", {})
        if not isinstance(overrides, dict):
            raise EngineError("Chain action_parameters must map action names to parameter objects.")
        names = {step["action"] for step in definition["actions"]}
        if set(overrides) - names:
            raise EngineError("Unknown action in action_parameters overrides.")
        shared = {key: value for key, value in parameters.items() if key != "action_parameters"}
        handlers = {}
        for name in names:
            if name not in actions:
                raise EngineError(f"Unknown action {name} in command.", status=404, code="action_not_found")
            handlers[name] = handler_for(actions[name]["handler"])
        allowed = set().union(*(handler.parameters for handler in handlers.values()))
        if set(shared) - allowed and not any(h.accepts_arbitrary_parameters for h in handlers.values()):
            raise EngineError("Unknown shared command parameters.")
        ids = {name: str(uuid.uuid4()) for name in names}
        calls = []
        for step in definition["actions"]:
            name = step["action"]
            handler = handlers[name]
            values = {
                key: copy.deepcopy(value) for key, value in shared.items()
                if key in handler.parameters or handler.accepts_arbitrary_parameters
            }
            override = overrides.get(name, {})
            if not isinstance(override, dict):
                raise EngineError("Each action_parameters override must be an object.")
            values.update(copy.deepcopy(override))
            # Bound arguments are always supplied by the command, not by callers.
            for parameter in step["parameter_bindings"]:
                values.pop(parameter, None)
            calls.append(action_step(
                actions, name, values, step["dependencies"], step["parameter_bindings"], ids,
            ))
        return calls
