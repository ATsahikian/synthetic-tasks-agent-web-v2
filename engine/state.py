import copy
import re
import uuid
from datetime import datetime

from .actions import fingerprint, validate_name, validate_request_id
from .errors import EngineError


OUTCOMES = {"succeeded", "failed", "cancelled", "timed_out"}


def empty_state(key):
    return {
        "schema_version": 1,
        "task_set": key[0],
        "task": key[1],
        "revision": 0,
        "actions": {},
        "current_actions": {},
        "latest_successful_actions": {},
        "ready_action_ids": [],
    }


def dependency_references(state, action):
    references = {}
    for name in action["dependencies"]:
        action_id = state["current_actions"].get(name)
        dependency = state["actions"].get(action_id)
        if dependency is None or dependency["outcome"] != "succeeded" or dependency["stale"]:
            raise EngineError(
                f"Action is blocked by dependency {name}.",
                status=409,
                code="dependencies_not_ready",
            )
        references[name] = action_id
    return references


def refresh_derived_state(state):
    actions = state["actions"]
    memo = {}

    def stale_reasons(action_id):
        if action_id in memo:
            return memo[action_id]
        action = actions[action_id]
        reasons = []
        for name, consumed_id in action["dependency_actions"].items():
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
        if action["status"] != "waiting":
            continue
        for name in action["dependencies"]:
            dependency = actions.get(state["current_actions"].get(name))
            if dependency is None or dependency["outcome"] != "succeeded" or dependency["stale"]:
                action["blocked_by"].append(name)
        action["waiting_reason"] = "dependencies" if action["blocked_by"] else "ready"
        if action["waiting_reason"] == "ready":
            state["ready_action_ids"].append(action_id)


def request_from_event(event):
    payload = event["payload"]
    if event["type"] == "waiting":
        return "command", {
            "request_id": event["request_id"],
            "action": payload["action"],
            "parameters": payload["parameters"],
        }
    if event["type"] == "cancel_requested":
        return "cancel", {"request_id": event["request_id"], "action_id": event["action_id"]}
    body = {
        "request_id": event["request_id"],
        "action_id": event["action_id"],
        "type": event["type"],
    }
    if event["type"] == "completed":
        body.update({name: payload[name] for name in ("outcome", "result", "error")})
    if event["type"] == "started" and event["request_id"] == f"engine:start:{event['action_id']}":
        return "start", body
    # Retain replay support for reports written before the server owned starts.
    return "report", body


def validate_event(event, revision):
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
        if not {"action", "parameters", "dependencies"}.issubset(payload) or set(payload) - {
            "action",
            "parameters",
            "dependencies",
            "execution",
            "execution_type",
            "resources",  # Ignored when replaying older event history.
            "invalidated_by",  # Ignored when replaying older event history.
        }:
            raise EngineError("Invalid waiting payload.")
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
    elif event["type"] == "cancel_requested":
        if payload:
            raise EngineError("Invalid cancellation payload.")
    else:
        expected = {"dependency_actions"}
        if event["type"] == "completed":
            expected |= {"outcome", "result", "error"}
            if not isinstance(payload.get("outcome"), str) or payload["outcome"] not in OUTCOMES:
                raise EngineError("Invalid completion outcome.")
            if payload.get("error") is not None and not isinstance(payload["error"], str):
                raise EngineError("Completion error must be a string or null.")
        if set(payload) != expected or not isinstance(payload["dependency_actions"], dict):
            raise EngineError("Invalid execution payload.")
    if not isinstance(event["request_fingerprint"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", event["request_fingerprint"]
    ):
        raise EngineError("Invalid request fingerprint.")
    operation, request = request_from_event(event)
    if fingerprint(operation, request) != event["request_fingerprint"]:
        raise EngineError("Event does not match its original request.")


def reduce_event(state, event):
    """Pure transition: no file access, configuration lookup, or job execution."""
    validate_event(event, state["revision"])
    state = copy.deepcopy(state)
    payload = event["payload"]
    action_id = event["action_id"]
    if event["type"] == "waiting":
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
    else:
        action = state["actions"].get(action_id)
        if action is None:
            raise EngineError("Unknown action ID.", status=404, code="action_not_found")
        if action["status"] == "completed":
            raise EngineError(
                "Action is already completed.", status=409, code="action_already_completed"
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
