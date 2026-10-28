"""Resolve the action class explicitly named by a definition or execution."""

from importlib import import_module
from inspect import isabstract, isclass
import re

from .base import ActionHandler
from ..errors import EngineError


HANDLER_NAME = re.compile(r"action_[a-z][a-z0-9_]{0,63}\Z")


def handler_for(name):
    if not isinstance(name, str) or not HANDLER_NAME.fullmatch(name):
        raise EngineError("handler must name an action class such as action_commit_desing.")
    try:
        module = import_module(f".{name}", __package__)
    except ModuleNotFoundError as error:
        if error.name != f"{__package__}.{name}":
            raise
        raise EngineError(f"Unknown action handler: {name}.") from error
    handler = getattr(module, name, None)
    if not isclass(handler) or not issubclass(handler, ActionHandler) or isabstract(handler):
        raise EngineError(f"Handler {name} must be a concrete ActionHandler class in {name}.py.")
    return handler()


def prepare_execution(definition, parameters):
    handler = handler_for(definition["handler"])
    spec = handler.prepare(definition.get("runner", {}), parameters)
    spec["handler"] = definition["handler"]
    return spec


def run_action(config, execute, output_path):
    return handler_for(config["execution"].get("handler")).run(config, execute, output_path)
