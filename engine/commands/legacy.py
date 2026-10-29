"""Replay and recovery for action histories written before command orchestration."""

from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, cast

from engine.json_data import object_value, text_value
from engine.type_defs import (
    ActionConfig,
    ActionState,
    Event,
    EventPayload,
    Receipt,
    RequestBody,
    State,
    TaskKey,
)

from ..actions import fingerprint, validate_name, validate_request_id
from ..errors import EngineError

if TYPE_CHECKING:
    from engine.core import Engine

OUTCOMES = {"succeeded", "failed", "cancelled", "timed_out"}
# Accept old mode metadata only when replaying historical payloads.
ACTION_FIELDS = {
    "action",
    "parameters",
    "dependencies",
    "execution",
    "execution_type",
    "mode",
}


def empty_state(key: TaskKey) -> State:
    return {
        "schema_version": 1,
        "task_set": key[0],
        "task": key[1],
        "revision": 0,
        "actions": {},
        "current_actions": {},
        "latest_successful_actions": {},
        "ready_action_ids": [],
        "commands": {},
        "active_command_id": None,
        "action_pool": [],
    }


def dependency_references(
    state: State, action: ActionState | EventPayload
) -> dict[str, str]:
    references = {}
    for name in action["dependencies"]:
        action_id = state["current_actions"].get(name)
        dependency = state["actions"].get(action_id) if action_id is not None else None
        if (
            dependency is None
            or dependency["outcome"] != "succeeded"
            or dependency["stale"]
        ):
            raise EngineError(
                f"Action is blocked by dependency {name}.",
                status=409,
                code="dependencies_not_ready",
            )
        assert action_id is not None
        references[name] = action_id
    return references


def refresh_derived_state(state: State) -> None:
    actions = state["actions"]
    memo: dict[str, list[str]] = {}

    def stale_reasons(action_id: str) -> list[str]:
        if action_id in memo:
            return memo[action_id]
        action = actions[action_id]
        reasons = []
        for name, consumed_id in action.get("dependency_actions", {}).items():
            if state["latest_successful_actions"].get(name) != consumed_id:
                reasons.append(f"{name} has a newer successful execution.")
            elif stale_reasons(consumed_id):
                reasons.append(f"{name} consumed stale inputs.")
        memo[action_id] = reasons
        return reasons

    for action_id, action in actions.items():
        action["stale_reasons"] = stale_reasons(action_id)
        action["stale"] = bool(action["stale_reasons"])
    state["ready_action_ids"] = []
    for action_id, action in actions.items():
        action["blocked_by"] = []
        action["waiting_reason"] = None
        if action["status"] != "waiting" or action.get("command_id"):
            continue
        for name in action["dependencies"]:
            dependency_id = state["current_actions"].get(name)
            dependency = (
                actions.get(dependency_id) if dependency_id is not None else None
            )
            if (
                dependency is None
                or dependency["outcome"] != "succeeded"
                or dependency["stale"]
            ):
                action["blocked_by"].append(name)
        action["waiting_reason"] = "dependencies" if action["blocked_by"] else "ready"
        if action["waiting_reason"] == "ready":
            state["ready_action_ids"].append(action_id)


def request_from_event(event: Event) -> tuple[str, RequestBody]:
    assert event["action_id"] is not None
    payload = event["payload"]
    if "action" in payload:
        return "command", {
            "request_id": event["request_id"],
            "action": payload["action"],
            "parameters": payload["parameters"],
        }
    if event["type"] == "cancel_requested":
        return "cancel", {
            "request_id": event["request_id"],
            "action_id": event["action_id"],
        }
    body: RequestBody = {
        "request_id": event["request_id"],
        "action_id": event["action_id"],
        "type": event["type"],
    }
    if event["type"] == "completed":
        body.update(
            {
                "outcome": payload["outcome"],
                "result": payload["result"],
                "error": payload["error"],
            }
        )
    if (
        event["type"] == "started"
        and event["request_id"] == f"engine:start:{event['action_id']}"
    ):
        return "start", body
    # Retain replay support for reports written before the server owned starts.
    return "report", body


def validate_action_payload(payload: EventPayload) -> None:
    if not {"action", "parameters", "dependencies"}.issubset(payload) or set(
        payload
    ) - (
        ACTION_FIELDS | {"resources", "invalidated_by"}  # Older accepted commands.
    ):
        raise EngineError("Invalid action payload.")
    validate_name(payload["action"])
    if not isinstance(payload["parameters"], dict):
        raise EngineError("Action parameters must be an object.")
    dependencies = payload["dependencies"]
    if not isinstance(dependencies, list):
        raise EngineError("Dependencies must be an array.")
    for dependency in dependencies:
        validate_name(dependency)
    if len(set(dependencies)) != len(dependencies) or payload["action"] in dependencies:
        raise EngineError("Invalid action dependencies.")
    if "execution" in payload and not isinstance(payload["execution"], dict):
        raise EngineError("Invalid execution specification.")
    if payload.get("execution_type", "script") not in ("script", "agent"):
        raise EngineError("Invalid execution type.")


def validate_event(event: Event, revision: int) -> None:
    required = {
        "schema_version",
        "sequence",
        "event_id",
        "request_id",
        "request_fingerprint",
        "action_id",
        "timestamp",
        "type",
        "payload",
    }
    if not isinstance(event, dict) or set(event) != required:
        raise EngineError("Event has an invalid envelope.")
    if type(event["schema_version"]) is not int or event["schema_version"] != 1:
        raise EngineError("Unsupported event schema.")
    if type(event["sequence"]) is not int or event["sequence"] != revision + 1:
        raise EngineError("Event sequences must be contiguous and start at 1.")
    validate_request_id(event["request_id"])
    for field in ("event_id", "action_id"):
        if not isinstance(event[field], str):
            raise EngineError(f"Invalid {field}.")
        try:
            if str(uuid.UUID(event[field])) != event[field]:
                raise ValueError("Noncanonical UUID")
        except ValueError as error:
            raise EngineError(f"Invalid {field}.") from error
    if not isinstance(event["timestamp"], str) or not event["timestamp"].endswith("Z"):
        raise EngineError("Invalid event timestamp.")
    try:
        datetime.fromisoformat(event["timestamp"])
    except ValueError as error:
        raise EngineError("Invalid event timestamp.") from error
    if event["type"] not in ("waiting", "started", "completed", "cancel_requested"):
        raise EngineError("Unknown event type.")
    payload = event["payload"]
    if not isinstance(payload, dict):
        raise EngineError("Event payload must be an object.")
    if event["type"] == "waiting":
        validate_action_payload(payload)
    elif event["type"] == "cancel_requested":
        if payload:
            raise EngineError("Invalid cancellation payload.")
    else:
        expected = {"dependency_actions"}
        if event["type"] == "completed":
            expected |= {"outcome", "result", "error"}
            if (
                not isinstance(payload.get("outcome"), str)
                or payload["outcome"] not in OUTCOMES
            ):
                raise EngineError("Invalid completion outcome.")
            if payload.get("error") is not None and not isinstance(
                payload["error"], str
            ):
                raise EngineError("Completion error must be a string or null.")
        if "action" in payload:
            fields = set(payload) & ACTION_FIELDS
            validate_action_payload(
                cast(
                    EventPayload,
                    {name: value for name, value in payload.items() if name in fields},
                )
            )
            expected |= fields
        if set(payload) != expected or not isinstance(
            payload.get("dependency_actions"), dict
        ):
            raise EngineError("Invalid execution payload.")
    if not isinstance(event["request_fingerprint"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", event["request_fingerprint"]
    ):
        raise EngineError("Invalid request fingerprint.")
    operation, request = request_from_event(event)
    if fingerprint(operation, request) != event["request_fingerprint"]:
        raise EngineError("Event does not match its original request.")


def reduce_event(state: State, event: Event) -> State:
    """Pure transition: no file access, configuration lookup, or job execution."""
    validate_event(event, state["revision"])
    state = copy.deepcopy(state)
    payload = event["payload"]
    action_id = event["action_id"]
    assert action_id is not None
    if "action" in payload:
        if action_id in state["actions"]:
            raise EngineError("Action ID was already used.")
        current_id = state["current_actions"].get(payload["action"])
        if current_id and state["actions"][current_id]["status"] != "completed":
            raise EngineError(
                "An invocation of this action is already active.",
                status=409,
                code="action_already_active",
            )
        state["actions"][action_id] = {
            "action_id": action_id,
            "name": payload["action"],
            "parameters": payload["parameters"],
            "dependencies": payload["dependencies"],
            "dependency_actions": {},
            "status": "waiting",
            "outcome": None,
            "result": None,
            "error": None,
            "requested_at": event["timestamp"],
            "started_at": None,
            "completed_at": None,
            "requested_sequence": event["sequence"],
            "completed_sequence": None,
            "execution": payload.get("execution"),
            "execution_type": payload.get("execution_type"),
            "cancel_requested": False,
        }
        state["current_actions"][payload["action"]] = action_id
    if event["type"] != "waiting":
        action = state["actions"].get(action_id)
        if action is None:
            raise EngineError("Unknown action ID.", status=404, code="action_not_found")
        if action["status"] == "completed":
            raise EngineError(
                "Action is already completed.",
                status=409,
                code="action_already_completed",
            )
        if event["type"] == "cancel_requested":
            action["cancel_requested"] = True
            if action["status"] == "waiting":
                action.update(
                    {
                        "status": "completed",
                        "outcome": "cancelled",
                        "completed_at": event["timestamp"],
                        "completed_sequence": event["sequence"],
                    }
                )
            state["revision"] = event["sequence"]
            refresh_derived_state(state)
            return state
        if event["type"] == "started" and action["status"] != "waiting":
            raise EngineError(
                "Action is already started.", status=409, code="action_already_started"
            )
        references = (
            dependency_references(state, action)
            if action["status"] == "waiting"
            else action["dependency_actions"]
        )
        if payload["dependency_actions"] != references:
            raise EngineError("Execution has incorrect dependency references.")
        action["dependency_actions"] = references
        if event["type"] == "started":
            action["status"] = "started"
            action["started_at"] = event["timestamp"]
        else:
            action.update(
                {
                    "status": "completed",
                    "outcome": payload["outcome"],
                    "result": payload["result"],
                    "error": payload["error"],
                    "completed_at": event["timestamp"],
                    "completed_sequence": event["sequence"],
                }
            )
            if payload["outcome"] == "succeeded":
                state["latest_successful_actions"][action["name"]] = action_id
    state["revision"] = event["sequence"]
    refresh_derived_state(state)
    return state


def start_ready(engine: Engine, key: TaskKey) -> None:
    """Persist starts before launching ready historical actions on workers."""
    while engine._states[key]["ready_action_ids"]:
        engine._check_available()
        action_id = engine._states[key]["ready_action_ids"][0]
        action = engine._states[key]["actions"][action_id]
        engine._commit(
            key,
            {
                "request_id": f"engine:start:{action_id}",
                "action_id": action_id,
                "type": "started",
            },
            "start",
            "started",
            action_id,
            {"dependency_actions": dependency_references(engine._states[key], action)},
        )
        engine._initiate(key, action_id)


def complete_action(engine: Engine, key: TaskKey, request: RequestBody) -> Receipt:
    engine._check_available()
    action = engine._states[key]["actions"].get(request["action_id"])
    if action is None:
        raise EngineError("Unknown action ID.", status=404, code="action_not_found")
    if action["status"] == "completed":
        raise EngineError(
            "Action is already completed.", status=409, code="action_already_completed"
        )
    if action["status"] != "started":
        raise EngineError(
            "Action has not been initiated by the server.",
            status=409,
            code="action_not_started",
        )
    return engine._commit(
        key,
        request,
        "report",
        "completed",
        request["action_id"],
        {
            "dependency_actions": copy.deepcopy(action["dependency_actions"]),
            "outcome": request["outcome"],
            "result": request["result"],
            "error": request["error"],
        },
    )


def cancel_action(engine: Engine, key: TaskKey, request: RequestBody) -> Receipt:
    action = engine._states[key]["actions"][request["action_id"]]
    receipt = engine._commit(
        key, request, "cancel", "cancel_requested", request["action_id"], {}
    )
    if action["status"] == "started":
        engine._runner.cancel(key, request["action_id"])
    engine._start_ready(key)
    return receipt


def adapt_configuration(config: ActionConfig) -> ActionConfig:
    """Translate frozen historical dependency results into the new call contract."""
    if "dependency_results" not in config:
        return config
    from pathlib import Path

    from ..actions.common import design_sources, read_document

    config = copy.deepcopy(config)
    action = config["action"]
    results = config.pop("dependency_results")
    handler = config["execution"].get("handler")
    producers = {
        "action_commit_desing": ("redesign",),
        "action_impl": ("commit_desing", "desing", "commit-desing", "approve_design"),
        "action_commit_impl": ("impl", "implement"),
        "action_local_bench": ("commit_impl", "impl", "commit-impl", "implement"),
        "action_local_bench_analyze": ("local_bench", "benchmark"),
        "action_export_to_codimango": ("impl", "implement", "local_bench_analyze"),
    }.get(handler or "", ())
    name = next((name for name in producers if name in results), None)
    if name is not None:
        source = object_value(copy.deepcopy(results[name] or {}))
        if handler == "action_local_bench" and name in ("impl", "implement"):
            source["approved"] = True
        if handler == "action_export_to_codimango" and name == "local_bench_analyze":
            read_document(text_value(source.get("report_path", "")))
            source["oracle_passed"] = True
        action["parameters"]["source"] = source
        if handler == "action_local_bench_analyze":
            action["parameters"]["source_id"] = action["dependency_actions"][name]
    elif handler == "action_impl":
        # Historical implementations could run directly against task documents.
        action["parameters"]["source"] = {
            **design_sources(Path(config["task_path"])),
            "approved": True,
        }
    return config


def capture_results(config: ActionConfig, state: State) -> ActionConfig:
    config["dependency_results"] = {
        name: copy.deepcopy(state["actions"][identity]["result"])
        for name, identity in config["action"]["dependency_actions"].items()
    }
    return config
