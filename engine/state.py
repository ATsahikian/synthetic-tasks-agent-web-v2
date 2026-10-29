"""Dispatch pure state transitions for current and historical event schemas."""

from __future__ import annotations

from engine.type_defs import Event, State, TaskKey

from .commands import legacy
from .commands.state import reduce_command_event, refresh_state


def empty_state(key: TaskKey) -> State:
    state = legacy.empty_state(key)
    state.update(
        {
            "schema_version": 2,
            "commands": {},
            "active_command_id": None,
            "action_pool": [],
        }
    )
    return state


def reduce_event(state: State, event: Event) -> State:
    if isinstance(event, dict) and event.get("schema_version") == 2:
        state = reduce_command_event(state, event)
    else:
        state = legacy.reduce_event(state, event)
    refresh_state(state)
    return state


# Only for histories created before command orchestration.
dependency_references = legacy.dependency_references
