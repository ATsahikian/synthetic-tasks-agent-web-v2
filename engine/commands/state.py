"""Pure command transitions, queue projection, and result provenance."""

import copy
import re
import uuid
from datetime import datetime

from .command_one_shot import command_one_shot
from ..actions import canonical_json, fingerprint, validate_name, validate_request_id
from ..errors import EngineError


OUTCOMES = {"succeeded", "failed", "cancelled", "timed_out"}
TYPES = {"command_started", "command_completed", "command_cancel_requested", "started", "completed", "cancelled"}


def valid_uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError) as error:
        raise EngineError("Invalid event UUID.") from error


def same_json(first, second):
    return canonical_json(first) == canonical_json(second)


def validate_event(event, revision):
    if set(event) != {
        "schema_version", "sequence", "event_id", "request_id", "request_fingerprint",
        "command_id", "action_id", "timestamp", "type", "payload",
    }:
        raise EngineError("Invalid command event envelope.")
    if type(event["schema_version"]) is not int or event["schema_version"] != 2:
        raise EngineError("Unsupported command event schema.")
    if type(event["sequence"]) is not int or event["sequence"] != revision + 1:
        raise EngineError("Event sequences must be contiguous and start at 1.")
    valid_uuid(event["event_id"])
    valid_uuid(event["command_id"])
    if not isinstance(event["type"], str) or event["type"] not in TYPES:
        raise EngineError("Unknown command event type.")
    if event["type"].startswith("command_"):
        if event["action_id"] is not None:
            raise EngineError("Command events cannot identify an action.")
    else:
        valid_uuid(event["action_id"])
    validate_request_id(event["request_id"])
    if not isinstance(event["timestamp"], str) or not event["timestamp"].endswith("Z"):
        raise EngineError("Invalid event timestamp.")
    try:
        datetime.fromisoformat(event["timestamp"])
    except ValueError as error:
        raise EngineError("Invalid event timestamp.") from error
    payload = event["payload"]
    if not isinstance(payload, dict) or not {"operation", "request"} <= set(payload):
        raise EngineError("Command event must capture its request.")
    if not isinstance(payload["request"], dict) or payload["request"].get("request_id") != event["request_id"]:
        raise EngineError("Event has incorrect request identity.")
    if not isinstance(payload["operation"], str):
        raise EngineError("Invalid event operation.")
    if not isinstance(event["request_fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", event["request_fingerprint"]):
        raise EngineError("Invalid request fingerprint.")
    if fingerprint(payload["operation"], payload["request"]) != event["request_fingerprint"]:
        raise EngineError("Event does not match its original request.")
    operation = payload["operation"]
    request = payload["request"]
    expected_operations = {
        "command_started": {"run_command", "command"},
        "command_completed": {"finish_command"},
        "command_cancel_requested": {"cancel"},
        "started": {"start"},
        "completed": {"report"},
        "cancelled": {"cancel_pending"},
    }
    if operation not in expected_operations[event["type"]]:
        raise EngineError("Incorrect operation for command event.")
    if event["type"] in ("started", "completed"):
        if request.get("action_id") != event["action_id"] or request.get("type") != event["type"]:
            raise EngineError("Incorrect action identity in execution request.")
        if event["type"] == "completed" and any(
            not same_json(request.get(name), payload.get(name)) for name in ("outcome", "result", "error")
        ):
            raise EngineError("Completion differs from its original request.")
    if event["type"] in ("command_completed", "cancelled"):
        names = ("outcome", "result", "error", "failed_action_id") if event["type"] == "command_completed" else ("error", "failed_action_id")
        if any(name not in request or not same_json(request[name], payload.get(name)) for name in names):
            raise EngineError("Command result differs from its original request.")
    if event["type"] == "command_started":
        if operation == "run_command":
            if request.get("command") != payload.get("command") or not same_json(request.get("parameters"), payload.get("parameters")):
                raise EngineError("Command differs from its original request.")
        elif payload.get("command") != "one-action" or not same_json(payload.get("parameters"), {
            "action": request.get("action"), "action_parameters": request.get("parameters"),
        }):
            raise EngineError("Legacy action wrapper differs from its original request.")
    fields = {
        "command_started": {"command", "parameters", "steps"},
        "command_completed": {"outcome", "result", "error", "failed_action_id"},
        "command_cancel_requested": set(),
        "started": {"parameters"},
        "completed": {"parameters", "outcome", "result", "error"},
        "cancelled": {"error", "failed_action_id"},
    }[event["type"]]
    if set(payload) != fields | {"operation", "request"}:
        raise EngineError("Invalid command event payload.")
    if "outcome" in payload and (not isinstance(payload["outcome"], str) or payload["outcome"] not in OUTCOMES):
        raise EngineError("Invalid completion outcome.")
    if "error" in payload and payload["error"] is not None and not isinstance(payload["error"], str):
        raise EngineError("Completion error must be text or null.")
    if "failed_action_id" in payload and payload["failed_action_id"] is not None:
        valid_uuid(payload["failed_action_id"])
    if "parameters" in payload and not isinstance(payload["parameters"], dict):
        raise EngineError("Parameters must be an object.")


def resolve_parameters(state, command, step):
    parameters = copy.deepcopy(step["parameters"])
    by_name = {item["action"]: item for item in command["steps"]}
    for dependency in step["dependencies"]:
        source = state["actions"][by_name[dependency]["action_id"]]
        if source["status"] != "completed" or source["outcome"] != "succeeded":
            raise EngineError("Command dependency has not succeeded.")
    for parameter, binding in step["parameter_bindings"].items():
        source = state["actions"][by_name[binding["step"]]["action_id"]]
        parameters[parameter] = copy.deepcopy(source[binding["value"]])
    return parameters


def reduce_command_event(state, event):
    validate_event(event, state["revision"])
    state = copy.deepcopy(state)
    payload = event["payload"]
    kind, command_id, action_id = event["type"], event["command_id"], event["action_id"]
    if kind == "command_started":
        if command_id in state["commands"] or state["active_command_id"]:
            raise EngineError("A command is already active.", status=409, code="command_already_active")
        if any(a["status"] != "completed" for a in state["actions"].values()):
            raise EngineError("A historical action is still active.", status=409, code="command_already_active")
        validate_name(payload["command"])
        steps = payload["steps"]
        if not isinstance(steps, list) or not steps:
            raise EngineError("Command execution plan must be nonempty.")
        seen_ids = set(state["actions"])
        for step in steps:
            # Older stored plans include execution mode metadata.
            if not isinstance(step, dict) or set(step) - {"mode"} != {
                "action_id", "action", "parameters", "dependencies", "parameter_bindings",
                "execution", "execution_type",
            }:
                raise EngineError("Invalid frozen command step.")
            valid_uuid(step["action_id"])
            if step["action_id"] in seen_ids:
                raise EngineError("Action ID was already used.")
            seen_ids.add(step["action_id"])
            if not isinstance(step["parameters"], dict) or not isinstance(step["execution"], dict):
                raise EngineError("Invalid action call.")
            if not isinstance(step["execution_type"], str) or step["execution_type"] not in ("agent", "script"):
                raise EngineError("Invalid action execution type.")
        command_one_shot().validate_definition({
            "handler": "command_one_shot",
            "actions": [{key: step[key] for key in ("action", "dependencies", "parameter_bindings")} for step in steps],
        })
        if payload["command"] == "one-action" and (
            len(steps) != 1 or steps[0]["dependencies"] or steps[0]["parameter_bindings"]
            or steps[0]["action"] != payload["parameters"].get("action")
            or not same_json(steps[0]["parameters"], payload["parameters"].get("action_parameters", {}))
        ):
            raise EngineError("Invalid one-action execution plan.")
        command = {
            "command_id": command_id, "name": payload["command"], "parameters": payload["parameters"],
            "steps": steps, "action_ids": [step["action_id"] for step in steps],
            "status": "started", "outcome": None, "result": None, "error": None,
            "failed_action_id": None, "cancel_requested": False,
            "started_at": event["timestamp"], "completed_at": None,
            "requested_sequence": event["sequence"], "completed_sequence": None,
        }
        state["commands"][command_id] = command
        state["active_command_id"] = command_id
        for step in steps:
            state["actions"][step["action_id"]] = {
                "action_id": step["action_id"], "command_id": command_id, "name": step["action"],
                "parameters": copy.deepcopy(step["parameters"]), "execution": step["execution"],
                "execution_type": step["execution_type"],
                "status": "queued", "outcome": None, "result": None, "error": None,
                "requested_at": event["timestamp"], "requested_sequence": event["sequence"],
                "started_at": None, "completed_at": None, "completed_sequence": None,
                "cancel_requested": False,
            }
    else:
        command = state["commands"].get(command_id)
        if command is None or command["status"] != "started" or state["active_command_id"] != command_id:
            raise EngineError("Command is not active.", status=409, code="command_not_active")
        actions = [state["actions"][identity] for identity in command["action_ids"]]
        if kind == "command_cancel_requested":
            if command["cancel_requested"]:
                raise EngineError("Command cancellation was already requested.", status=409)
            command["cancel_requested"] = True
            for action in actions:
                if action["status"] == "started":
                    action["cancel_requested"] = True
        elif kind == "command_completed":
            if any(action["status"] != "completed" for action in actions):
                raise EngineError("Command still has unfinished actions.")
            if payload["outcome"] == "succeeded" and (
                command["cancel_requested"] or any(a["outcome"] != "succeeded" for a in actions)
            ):
                raise EngineError("Command cannot report success.")
            if payload["failed_action_id"] is not None and payload["failed_action_id"] not in command["action_ids"]:
                raise EngineError("Invalid failed action reference.")
            command.update({key: payload[key] for key in ("outcome", "result", "error", "failed_action_id")})
            command.update({"status": "completed", "completed_at": event["timestamp"], "completed_sequence": event["sequence"]})
            state["active_command_id"] = None
        else:
            if action_id not in command["action_ids"]:
                raise EngineError("Action does not belong to this command.")
            action = state["actions"][action_id]
            step = next(step for step in command["steps"] if step["action_id"] == action_id)
            if kind == "cancelled":
                if action["status"] != "queued":
                    raise EngineError("Only queued actions can be cancelled without execution.")
                action.update({"status": "completed", "outcome": "cancelled", "error": payload["error"]})
            else:
                unfinished = [a for a in actions if a["status"] != "completed"]
                if not unfinished or unfinished[0]["action_id"] != action_id:
                    raise EngineError("Only the command's next action can execute.")
                if command["cancel_requested"] and action["status"] == "queued":
                    raise EngineError("Cancelled commands cannot start new work.")
                if any(a["outcome"] != "succeeded" for a in actions[:actions.index(action)]):
                    raise EngineError("Command cannot execute after a failed action.")
                if not same_json(payload["parameters"], resolve_parameters(state, command, step)):
                    raise EngineError("Incorrect action parameters for command step.")
                if kind == "started":
                    if action["status"] != "queued":
                        raise EngineError("Invalid action start.")
                    action.update({"status": "started", "started_at": event["timestamp"]})
                else:
                    # Historical plans could complete a queued step directly.
                    historical_completion = step.get("mode") == "sync" and action["status"] == "queued"
                    if action["status"] != "started" and not historical_completion:
                        raise EngineError("Invalid action completion.")
                    action.update({key: payload[key] for key in ("outcome", "result", "error")})
                    action["status"] = "completed"
                    if action["outcome"] == "succeeded":
                        state["latest_successful_actions"][action["name"]] = action_id
                action["parameters"] = payload["parameters"]
                state["current_actions"][action["name"]] = action_id
            if action["status"] == "completed":
                action.update({"completed_at": event["timestamp"], "completed_sequence": event["sequence"]})
    state["revision"] = event["sequence"]
    return state


def refresh_state(state):
    """Project queue and freshness without consulting current configuration."""
    active = state["commands"].get(state["active_command_id"])
    state["action_pool"] = [] if active is None else [
        {"command_id": active["command_id"], **copy.deepcopy(step)}
        for step in active["steps"] if state["actions"][step["action_id"]]["status"] == "queued"
    ]
    references = {}
    for command in state["commands"].values():
        ids = {step["action"]: step["action_id"] for step in command["steps"]}
        for step in command["steps"]:
            references[step["action_id"]] = {name: ids[name] for name in step["dependencies"]}
    memo = {}

    def stale(action_id):
        if action_id in memo:
            return memo[action_id]
        action = state["actions"][action_id]
        reasons = []
        if action["status"] in ("started", "completed"):
            for name, source_id in references.get(action_id, action.get("dependency_actions", {})).items():
                source = state["actions"][source_id]
                if source["outcome"] != "succeeded":
                    continue
                if state["latest_successful_actions"].get(name) != source_id:
                    reasons.append(f"{name} has a newer successful execution.")
                elif stale(source_id):
                    reasons.append(f"{name} consumed stale inputs.")
        memo[action_id] = reasons
        return reasons

    for identity, action in state["actions"].items():
        action["stale_reasons"] = stale(identity)
        action["stale"] = bool(action["stale_reasons"])
