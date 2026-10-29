"""A generic debugging command with exactly one caller-supplied action call."""

from .base import CommandHandler, action_step
from ..errors import EngineError


class command_one_action(CommandHandler):
    def validate_definition(self, definition):
        if set(definition) - {"handler", "label", "description"}:
            raise EngineError("one-action cannot define invocation options or bindings.")

    def prepare(self, definition, actions, parameters):
        if "action" not in parameters or set(parameters) - {"action", "action_parameters"}:
            raise EngineError("one-action requires action and optionally action_parameters.")
        return [action_step(actions, parameters["action"], parameters.get("action_parameters", {}))]
