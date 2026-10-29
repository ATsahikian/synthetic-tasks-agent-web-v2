"""Pure command transitions, queue projection, and result provenance."""

from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime

from engine.type_defs import (
    ActionState,
    ActionStep,
    CommandState,
    Event,
    JsonObject,
    State,
)

from ..actions import canonical_json, fingerprint, validate_name, validate_request_id
from ..actions.recovery import MODELS, is_exhausted, retry_decision, validate_failure
from ..errors import EngineError
from .command_one_shot import command_one_shot

OUTCOMES = {"succeeded", "failed", "cancelled", "timed_out"}
TYPES = {
    "command_started",
    "command_completed",
    "command_cancel_requested",
    "started",
    "completed",
    "cancelled",
    "retry_scheduled",
    "recovery_exhausted",
}


def valid_uuid(value: object) -> None:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError) as error:
        raise EngineError("Invalid event UUID.") from error


def same_json(first: object, second: object) -> bool:
    return canonical_json(first) == canonical_json(second)


def validate_event(event: Event, revision: int) -> None:
    if set(event) != {
        "schema_version",
        "sequence",
        "event_id",
        "request_id",
        "request_fingerprint",
        "command_id",
        "action_id",
        "timestamp",
        "type",
        "payload",
    }:
        raise EngineError("Invalid command event envelope.")
    if type(event["schema_version"]) is not int or event["schema_version"] not in (
        2,
        3,
    ):
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
    if (
        not isinstance(payload["request"], dict)
        or payload["request"].get("request_id") != event["request_id"]
    ):
        raise EngineError("Event has incorrect request identity.")
    if not isinstance(payload["operation"], str):
        raise EngineError("Invalid event operation.")
    if not isinstance(event["request_fingerprint"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", event["request_fingerprint"]
    ):
        raise EngineError("Invalid request fingerprint.")
    if (
        fingerprint(payload["operation"], payload["request"])
        != event["request_fingerprint"]
    ):
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
        "retry_scheduled": {"schedule_retry"},
        "recovery_exhausted": {"exhaust_recovery"},
    }
    if operation not in expected_operations[event["type"]]:
        raise EngineError("Incorrect operation for command event.")
    if event["type"] in ("started", "completed"):
        if (
            request.get("action_id") != event["action_id"]
            or request.get("type") != event["type"]
        ):
            raise EngineError("Incorrect action identity in execution request.")
        if event["type"] == "completed" and any(
            not same_json(request.get(name), payload.get(name))
            for name in ("outcome", "result", "error")
        ):
            raise EngineError("Completion differs from its original request.")
    if event["type"] in ("command_completed", "cancelled"):
        names = (
            ("outcome", "result", "error", "failed_action_id")
            if event["type"] == "command_completed"
            else ("error", "failed_action_id")
        )
        if any(
            name not in request or not same_json(request.get(name), payload.get(name))
            for name in names
        ):
            raise EngineError("Command result differs from its original request.")
    if event["type"] == "command_started":
        if operation == "run_command":
            if request.get("command") != payload.get("command") or not same_json(
                request.get("parameters"), payload.get("parameters")
            ):
                raise EngineError("Command differs from its original request.")
        elif payload.get("command") != "one-action" or not same_json(
            payload.get("parameters"),
            {
                "action": request.get("action"),
                "action_parameters": request.get("parameters"),
            },
        ):
            raise EngineError(
                "Legacy action wrapper differs from its original request."
            )
    fields = {
        "command_started": {"command", "parameters", "steps"},
        "command_completed": {"outcome", "result", "error", "failed_action_id"},
        "command_cancel_requested": set(),
        "started": {"parameters"},
        "completed": {"parameters", "outcome", "result", "error"},
        "cancelled": {"error", "failed_action_id"},
        "retry_scheduled": {
            "root_action_id",
            "predecessor_action_id",
            "model",
            "attempt_number",
            "reason",
            "execute_at",
            "execution",
            "parameters",
        },
        "recovery_exhausted": {
            "root_action_id",
            "predecessor_action_id",
            "model",
            "attempt_number",
            "reason",
        },
    }[event["type"]]
    if event["schema_version"] == 3 and event["type"] == "completed":
        fields.add("failure")
        validate_failure(payload.get("failure"))
        if not same_json(request.get("failure"), payload.get("failure")):
            raise EngineError("Failure differs from its original request.")
    if event["schema_version"] == 2 and event["type"] in (
        "retry_scheduled",
        "recovery_exhausted",
    ):
        raise EngineError("Historical commands cannot schedule recovery.")
    if set(payload) != fields | {"operation", "request"}:
        raise EngineError("Invalid command event payload.")
    if "outcome" in payload and (
        not isinstance(payload["outcome"], str) or payload["outcome"] not in OUTCOMES
    ):
        raise EngineError("Invalid completion outcome.")
    if (
        "error" in payload
        and payload["error"] is not None
        and not isinstance(payload["error"], str)
    ):
        raise EngineError("Completion error must be text or null.")
    if "failed_action_id" in payload and payload["failed_action_id"] is not None:
        valid_uuid(payload["failed_action_id"])
    if "parameters" in payload and not isinstance(payload["parameters"], dict):
        raise EngineError("Parameters must be an object.")


def resolve_parameters(
    state: State, command: CommandState, step: ActionStep
) -> JsonObject:
    parameters = copy.deepcopy(step["parameters"])
    by_name = {item["action"]: item for item in command["steps"]}
    for dependency in step["dependencies"]:
        source = state["actions"][
            resolved_id(command, by_name[dependency]["action_id"])
        ]
        if source["status"] != "completed" or source["outcome"] != "succeeded":
            raise EngineError("Command dependency has not succeeded.")
    for parameter, binding in step["parameter_bindings"].items():
        source = state["actions"][
            resolved_id(command, by_name[binding["step"]]["action_id"])
        ]
        parameters[parameter] = copy.deepcopy(source[binding["value"]])
    return parameters


def resolved_id(command: CommandState, root_id: str) -> str:
    return command.get("step_action_ids", {}).get(root_id, root_id)


def effective_actions(state: State, command: CommandState) -> list[ActionState]:
    return [
        state["actions"][resolved_id(command, step["action_id"])]
        for step in command["steps"]
    ]


def original_step(command: CommandState, action: ActionState) -> ActionStep:
    root_id = action.get("root_action_id", action["action_id"])
    return next(step for step in command["steps"] if step["action_id"] == root_id)


def attempt_parameters(
    state: State, command: CommandState, action: ActionState
) -> JsonObject:
    if "predecessor_action_id" in action:
        return copy.deepcopy(action["parameters"])
    return resolve_parameters(state, command, original_step(command, action))


def reduce_command_event(state: State, event: Event) -> State:
    validate_event(event, state["revision"])
    state = copy.deepcopy(state)
    payload = event["payload"]
    kind, command_id, action_id = event["type"], event["command_id"], event["action_id"]
    if kind == "command_started":
        if command_id in state["commands"] or state["active_command_id"]:
            raise EngineError(
                "A command is already active.",
                status=409,
                code="command_already_active",
            )
        if any(a["status"] != "completed" for a in state["actions"].values()):
            raise EngineError(
                "A historical action is still active.",
                status=409,
                code="command_already_active",
            )
        validate_name(payload["command"])
        steps = payload["steps"]
        if not isinstance(steps, list) or not steps:
            raise EngineError("Command execution plan must be nonempty.")
        seen_ids = set(state["actions"])
        for step in steps:
            # Older stored plans include execution mode metadata.
            if not isinstance(step, dict) or set(step) - {"mode"} != {
                "action_id",
                "action",
                "parameters",
                "dependencies",
                "parameter_bindings",
                "execution",
                "execution_type",
            }:
                raise EngineError("Invalid frozen command step.")
            valid_uuid(step["action_id"])
            if step["action_id"] in seen_ids:
                raise EngineError("Action ID was already used.")
            seen_ids.add(step["action_id"])
            if not isinstance(step["parameters"], dict) or not isinstance(
                step["execution"], dict
            ):
                raise EngineError("Invalid action call.")
            if not isinstance(step["execution_type"], str) or step[
                "execution_type"
            ] not in ("agent", "script"):
                raise EngineError("Invalid action execution type.")
        command_one_shot().validate_definition(
            {
                "handler": "command_one_shot",
                "actions": [
                    {
                        "action": step["action"],
                        "dependencies": step["dependencies"],
                        "parameter_bindings": step["parameter_bindings"],
                    }
                    for step in steps
                ],
            }
        )
        if payload["command"] == "one-action" and (
            len(steps) != 1
            or steps[0]["dependencies"]
            or steps[0]["parameter_bindings"]
            or steps[0]["action"] != payload["parameters"].get("action")
            or not same_json(
                steps[0]["parameters"],
                payload["parameters"].get("action_parameters", {}),
            )
        ):
            raise EngineError("Invalid one-action execution plan.")
        command: CommandState = {
            "command_id": command_id,
            "name": payload["command"],
            "parameters": payload["parameters"],
            "steps": steps,
            "action_ids": [step["action_id"] for step in steps],
            "status": "started",
            "outcome": None,
            "result": None,
            "error": None,
            "failed_action_id": None,
            "cancel_requested": False,
            "started_at": event["timestamp"],
            "completed_at": None,
            "requested_sequence": event["sequence"],
            "completed_sequence": None,
            "schema_version": event["schema_version"],
        }
        if event["schema_version"] == 3:
            command["step_action_ids"] = {
                step["action_id"]: step["action_id"] for step in steps
            }
        state["commands"][command_id] = command
        state["active_command_id"] = command_id
        for step in steps:
            state["actions"][step["action_id"]] = {
                "action_id": step["action_id"],
                "command_id": command_id,
                "name": step["action"],
                "parameters": copy.deepcopy(step["parameters"]),
                "execution": step["execution"],
                "execution_type": step["execution_type"],
                "status": "queued",
                "outcome": None,
                "result": None,
                "error": None,
                "requested_at": event["timestamp"],
                "requested_sequence": event["sequence"],
                "started_at": None,
                "completed_at": None,
                "completed_sequence": None,
                "cancel_requested": False,
            }
            if event["schema_version"] == 3:
                state["actions"][step["action_id"]].update(
                    {
                        "root_action_id": step["action_id"],
                        "attempt_number": 1,
                        "failure": None,
                    }
                )
    else:
        saved_command = state["commands"].get(command_id)
        if (
            saved_command is None
            or saved_command["status"] != "started"
            or state["active_command_id"] != command_id
        ):
            raise EngineError(
                "Command is not active.", status=409, code="command_not_active"
            )
        command = saved_command
        if event["schema_version"] != command.get("schema_version", 2):
            raise EngineError("Command event schema changed during execution.")
        actions = [state["actions"][identity] for identity in command["action_ids"]]
        if kind == "command_cancel_requested":
            if command["cancel_requested"]:
                raise EngineError(
                    "Command cancellation was already requested.", status=409
                )
            command["cancel_requested"] = True
            for action in actions:
                if action["status"] == "started":
                    action["cancel_requested"] = True
        elif kind == "command_completed":
            if any(action["status"] != "completed" for action in actions):
                raise EngineError("Command still has unfinished actions.")
            if payload["outcome"] == "succeeded" and (
                command["cancel_requested"]
                or any(
                    a["outcome"] != "succeeded"
                    for a in effective_actions(state, command)
                )
            ):
                raise EngineError("Command cannot report success.")
            if (
                payload["failed_action_id"] is not None
                and payload["failed_action_id"] not in command["action_ids"]
            ):
                raise EngineError("Invalid failed action reference.")
            command.update(
                {
                    "outcome": payload["outcome"],
                    "result": payload["result"],
                    "error": payload["error"],
                    "failed_action_id": payload["failed_action_id"],
                }
            )
            command.update(
                {
                    "status": "completed",
                    "completed_at": event["timestamp"],
                    "completed_sequence": event["sequence"],
                }
            )
            state["active_command_id"] = None
        elif kind in ("retry_scheduled", "recovery_exhausted"):
            root_id, predecessor_id = (
                payload["root_action_id"],
                payload["predecessor_action_id"],
            )
            if command["cancel_requested"] or root_id not in command.get(
                "step_action_ids", {}
            ):
                raise EngineError("Command cannot schedule recovery.")
            predecessor = state["actions"].get(predecessor_id)
            if (
                predecessor is None
                or resolved_id(command, root_id) != predecessor_id
                or predecessor.get("root_action_id") != root_id
            ):
                raise EngineError("Invalid retry predecessor.")
            decision = retry_decision(predecessor)
            if kind == "recovery_exhausted":
                spec, failure = predecessor["execution"], predecessor.get("failure")
                if (
                    not is_exhausted(predecessor)
                    or predecessor.get("recovery_exhausted")
                    or action_id != predecessor_id
                ):
                    raise EngineError("Invalid recovery exhaustion.")
                assert spec is not None and failure is not None
                if (
                    payload["model"] != MODELS[spec["agent"]]
                    or payload["attempt_number"] != predecessor["attempt_number"]
                    or payload["reason"] != failure["classification"]
                ):
                    raise EngineError("Incorrect exhaustion diagnostics.")
                predecessor["recovery_exhausted"] = True
            else:
                if decision is None or any(
                    not same_json(payload.get(name), value)
                    for name, value in decision.items()
                ):
                    raise EngineError(
                        "Retry differs from the captured recovery policy."
                    )
                if action_id in state["actions"] or not same_json(
                    payload["parameters"], predecessor["parameters"]
                ):
                    raise EngineError("Invalid replacement action call.")
                assert action_id is not None
                replacement = copy.deepcopy(predecessor)
                replacement.update(
                    {
                        "action_id": action_id,
                        "predecessor_action_id": predecessor_id,
                        "execution": payload["execution"],
                        "attempt_number": payload["attempt_number"],
                        "execute_at": payload["execute_at"],
                        "status": "queued",
                        "outcome": None,
                        "result": None,
                        "error": None,
                        "failure": None,
                        "requested_at": event["timestamp"],
                        "requested_sequence": event["sequence"],
                        "started_at": None,
                        "completed_at": None,
                        "completed_sequence": None,
                    }
                )
                replacement.pop("recovery_exhausted", None)
                state["actions"][action_id] = replacement
                command["action_ids"].insert(
                    command["action_ids"].index(predecessor_id) + 1, action_id
                )
                command["step_action_ids"][root_id] = action_id
        else:
            if action_id not in command["action_ids"]:
                raise EngineError("Action does not belong to this command.")
            assert action_id is not None
            action = state["actions"][action_id]
            step = original_step(command, action)
            if kind == "cancelled":
                if action["status"] != "queued":
                    raise EngineError(
                        "Only queued actions can be cancelled without execution."
                    )
                action.update(
                    {
                        "status": "completed",
                        "outcome": "cancelled",
                        "error": payload["error"],
                    }
                )
            else:
                effective = effective_actions(state, command)
                unfinished = [a for a in effective if a["status"] != "completed"]
                if not unfinished or unfinished[0]["action_id"] != action_id:
                    raise EngineError("Only the command's next action can execute.")
                if command["cancel_requested"] and action["status"] == "queued":
                    raise EngineError("Cancelled commands cannot start new work.")
                if any(
                    a["outcome"] != "succeeded"
                    for a in effective[: effective.index(action)]
                ):
                    raise EngineError("Command cannot execute after a failed action.")
                if not same_json(
                    payload["parameters"], attempt_parameters(state, command, action)
                ):
                    raise EngineError("Incorrect action parameters for command step.")
                if kind == "started":
                    if action["status"] != "queued":
                        raise EngineError("Invalid action start.")
                    if action.get("execute_at") and datetime.fromisoformat(
                        event["timestamp"]
                    ) < datetime.fromisoformat(action["execute_at"]):
                        raise EngineError(
                            "Delayed retry cannot start before its execution time."
                        )
                    action.update(
                        {"status": "started", "started_at": event["timestamp"]}
                    )
                else:
                    # Historical plans could complete a queued step directly.
                    historical_completion = (
                        step.get("mode") == "sync" and action["status"] == "queued"
                    )
                    if action["status"] != "started" and not historical_completion:
                        raise EngineError("Invalid action completion.")
                    action.update(
                        {
                            "outcome": payload["outcome"],
                            "result": payload["result"],
                            "error": payload["error"],
                        }
                    )
                    action["status"] = "completed"
                    if event["schema_version"] == 3:
                        action["failure"] = payload["failure"]
                    if action["outcome"] == "succeeded":
                        state["latest_successful_actions"][action["name"]] = action_id
                action["parameters"] = payload["parameters"]
                state["current_actions"][action["name"]] = action_id
            if action["status"] == "completed":
                action.update(
                    {
                        "completed_at": event["timestamp"],
                        "completed_sequence": event["sequence"],
                    }
                )
    state["revision"] = event["sequence"]
    return state


def refresh_state(state: State) -> None:
    """Project queue and freshness without consulting current configuration."""
    active_id = state["active_command_id"]
    active = state["commands"].get(active_id) if active_id is not None else None
    state["action_pool"] = []
    if active is not None:
        for identity in active["action_ids"]:
            action = state["actions"][identity]
            if action["status"] != "queued":
                continue
            step = copy.deepcopy(original_step(active, action))
            step.update({"command_id": active["command_id"], "action_id": identity})
            if "predecessor_action_id" in action:
                assert action["execution"] is not None
                step.update(
                    {
                        "root_action_id": action["root_action_id"],
                        "predecessor_action_id": action["predecessor_action_id"],
                        "attempt_number": action["attempt_number"],
                        "execute_at": action["execute_at"],
                        "parameters": copy.deepcopy(action["parameters"]),
                        "execution": copy.deepcopy(action["execution"]),
                    }
                )
            state["action_pool"].append(step)
    references = {}
    for command in state["commands"].values():
        ids = {
            step["action"]: resolved_id(command, step["action_id"])
            for step in command["steps"]
        }
        for identity in command["action_ids"]:
            step = original_step(command, state["actions"][identity])
            references[identity] = {name: ids[name] for name in step["dependencies"]}
    memo: dict[str, list[str]] = {}

    def stale(action_id: str) -> list[str]:
        if action_id in memo:
            return memo[action_id]
        action = state["actions"][action_id]
        reasons = []
        if action["status"] in ("started", "completed"):
            for name, source_id in references.get(
                action_id, action.get("dependency_actions", {})
            ).items():
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
