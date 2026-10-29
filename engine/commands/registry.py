"""Resolve the handler named by a command definition."""

from .command_one_action import command_one_action
from .command_one_shot import command_one_shot
from ..errors import EngineError


HANDLERS = {
    "command_one_action": command_one_action,
    "command_one_shot": command_one_shot,
}


def handler_for(name):
    if not isinstance(name, str) or name not in HANDLERS:
        raise EngineError("Unknown command handler.")
    return HANDLERS[name]()
