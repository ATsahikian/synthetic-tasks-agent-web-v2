import copy
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .actions import (
    canonical_json, fingerprint, load_actions, run_action, validate_request_id,
)
from .discovery import discover_tasks, validate_component
from .errors import CorruptLogError, EngineError
from .state import empty_state, reduce_event
from .commands import CommandScheduler, load_commands
from .commands import legacy
from .storage import EventStore


LOGGER = logging.getLogger(__name__)
ENGINE_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_TASKS_ROOT = ENGINE_DIRECTORY.parent.parent / "synthetic-tasks-project-v2"
INTERNAL_REQUEST_PREFIX = "engine:"


def validate_body(body, required, optional=()):
    if not isinstance(body, dict):
        raise EngineError("Request body must be a JSON object.")
    if not set(required).issubset(body) or set(body) - set(required) - set(optional):
        raise EngineError(
            f"Expected fields: {', '.join(required)}."
            + (f" Optional: {', '.join(optional)}." if optional else "")
        )
    validate_request_id(body["request_id"])
    if body["request_id"].startswith(INTERNAL_REQUEST_PREFIX):
        raise EngineError("request_id must not use the reserved engine: prefix.")
    try:
        canonical_json(body).encode("utf-8")
    except (ValueError, UnicodeError, TypeError, RecursionError) as error:
        raise EngineError("Request must contain valid finite JSON values.") from error


def acknowledgment(key, event):
    receipt = {
        "task_set": key[0],
        "task": key[1],
        "action_id": event["action_id"],
        "event_id": event["event_id"],
        "sequence": event["sequence"],
        "type": event["type"],
    }
    if event.get("command_id"):
        receipt["command_id"] = event["command_id"]
        if event["type"] == "command_started":
            receipt["action_ids"] = [step["action_id"] for step in event["payload"]["steps"]]
            if len(receipt["action_ids"]) == 1:
                receipt["action_id"] = receipt["action_ids"][0]
            else:
                receipt.pop("action_id")
    if event["type"] in ("completed", "command_completed"):
        receipt.update({
            name: copy.deepcopy(event["payload"][name])
            for name in ("outcome", "result", "error")
        })
    if event["type"] == "command_completed":
        receipt["failed_action_id"] = event["payload"]["failed_action_id"]
    return receipt


class Engine:
    def __init__(
        self,
        tasks_root=DEFAULT_TASKS_ROOT,
        *,
        actions_file=None,
        commands=None,
    ):
        self._lock = threading.RLock()
        self._closed = False
        self._stopping = False
        self._unavailable = False
        self._store = None
        self._runner = None
        self.catalog, self._discovered = discover_tasks(tasks_root)
        self.actions = load_actions(actions_file or ENGINE_DIRECTORY / "actions" / "actions.json")
        self.commands = load_commands(commands)
        self._scheduler = CommandScheduler(self)
        self._states = {}
        self._events = {}
        self._requests = {}
        try:
            self._store = EventStore(tasks_root)
            keys = self._discovered | self._store.task_keys()
            # Acquire every task lock before replaying or updating any stored state.
            for key in sorted(keys):
                self._store.ensure_task(key)
            task_sets = {entry["name"] for entry in self.catalog["task_sets"]} | {
                key[0] for key in keys
            }
            for task_set in sorted(task_sets):
                self._store.ensure_workspace(task_set)
            for key in sorted(keys):
                events = self._store.read_events(key)
                state = empty_state(key)
                requests, event_ids = {}, set()
                for index, event in enumerate(events, 1):
                    try:
                        state = reduce_event(state, event)
                        if event["request_id"] in requests or event["event_id"] in event_ids:
                            raise EngineError("Duplicate request ID or event ID in history.")
                        requests[event["request_id"]] = event
                        event_ids.add(event["event_id"])
                    except (EngineError, ValueError, TypeError, KeyError, RecursionError) as error:
                        raise CorruptLogError(
                            f"Cannot replay {key[0]}/{key[1]} event {index}: {error}"
                        ) from error
                self._states[key] = state
                self._events[key] = events
                self._requests[key] = requests
                self._save_snapshot(key)
            from .runner import ActionRunner

            self._runner = ActionRunner(self)
            self._runner.recover()
            # Resume commands left ready by a crash, without relaunching started actions.
            with self._lock:
                for key in sorted(self._states):
                    self._start_ready(key)
            self._runner.start_monitor()
        except BaseException:
            if self._runner is not None:
                self._runner.close()
            if self._store is not None:
                self._store.close()
            raise

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._stopping = True
        try:
            if self._runner is not None:
                self._runner.close()
        finally:
            with self._lock:
                self._closed = True
                if self._store is not None:
                    self._store.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _check_available(self):
        if self._closed or self._unavailable:
            raise EngineError(
                "Engine is unavailable. Restart it and retry with the same request_id.",
                status=503,
                code="engine_unavailable",
            )

    def _key(self, task_set, task):
        key = (validate_component(task_set), validate_component(task))
        if key not in self._states:
            raise EngineError("Unknown task.", status=404, code="task_not_found")
        return key

    def list_tasks(self):
        with self._lock:
            return copy.deepcopy(self.catalog)

    def list_actions(self):
        from .actions.registry import handler_for

        with self._lock:
            actions = copy.deepcopy(self.actions)
            for definition in actions.values():
                handler = handler_for(definition["handler"])
                definition["parameters"] = sorted(handler.parameters)
                definition["required_parameters"] = sorted(handler.required_parameters)
                definition["accepts_arbitrary_parameters"] = handler.accepts_arbitrary_parameters
            return {"schema_version": 1, "actions": actions}

    def list_commands(self):
        with self._lock:
            return {"schema_version": 1, "commands": copy.deepcopy(self.commands)}

    def health(self):
        with self._lock:
            self._check_available()
            return {
                "status": "ok",
                "task_sets": len(self.catalog["task_sets"]),
                "tasks": len(self._discovered),
            }

    def state(self, task_set, task):
        with self._lock:
            key = self._key(task_set, task)
            return copy.deepcopy(self._states[key])

    def events(self, task_set, task, *, after=0):
        if type(after) is not int or after < 0:
            raise EngineError("after must be a nonnegative integer sequence.")
        with self._lock:
            key = self._key(task_set, task)
            return {
                "task_set": task_set,
                "task": task,
                "revision": self._states[key]["revision"],
                "events": copy.deepcopy(self._events[key][after:]),
            }

    def output(self, task_set, task, action_id, *, after=0):
        if type(after) is not int or after < 0 or after > 2**63 - 1:
            raise EngineError("after must be a nonnegative byte offset.")
        with self._lock:
            key = self._key(task_set, task)
            if action_id not in self._states[key]["actions"]:
                raise EngineError("Unknown action ID.", status=404, code="action_not_found")
            from .runner import read_output

            return read_output(
                Path(self.catalog["tasks_root"]),
                key,
                action_id,
                after,
                final=self._states[key]["actions"][action_id]["status"] == "completed",
            )

    def _duplicate(self, key, request_id, request_fingerprint):
        previous = self._requests[key].get(request_id)
        if previous is None:
            return None
        if previous["request_fingerprint"] != request_fingerprint:
            raise EngineError(
                "request_id was already used for a different request.",
                status=409,
                code="request_id_conflict",
            )
        return acknowledgment(key, previous)

    def _ensure_discovered(self, key):
        if key not in self._discovered:
            raise EngineError(
                "Task is no longer present in the input catalog.",
                status=404,
                code="task_not_found",
            )

    def _save_snapshot(self, key):
        for name, write in (("job.json", self._store.write_snapshot), ("action-pool.json", self._store.write_pool)):
            try:
                write(key, self._states[key])
            except OSError:
                # Events remain authoritative; projection writes are retried on later events/restart.
                LOGGER.exception("Could not update %s for %s/%s", name, *key)

    def _commit_command(self, key, request, operation, event_type, command_id, action_id, payload):
        return self._commit(
            key, request, operation, event_type, action_id,
            {"operation": operation, "request": copy.deepcopy(request), **payload},
            command_id=command_id,
        )

    def _commit(self, key, request, operation, event_type, action_id, payload, *, command_id=None):
        event = {
            "schema_version": 2 if command_id else 1,
            "sequence": self._states[key]["revision"] + 1,
            "event_id": str(uuid.uuid4()),
            "request_id": request["request_id"],
            "request_fingerprint": fingerprint(operation, request),
            "action_id": action_id,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": event_type,
            "payload": payload,
        }
        if command_id is not None:
            event["command_id"] = command_id
        next_state = reduce_event(self._states[key], event)
        try:
            self._store.append(key, event)
        except OSError as error:
            # A failed fsync has an uncertain result. Stop mutations until startup replay.
            self._unavailable = True
            LOGGER.exception("Could not durably append an event for %s/%s", *key)
            raise EngineError(
                "Event persistence failed. Restart and retry with the same request_id.",
                status=503,
                code="event_persistence_failed",
            ) from error
        self._states[key] = next_state
        self._events[key].append(event)
        self._requests[key][event["request_id"]] = event
        self._save_snapshot(key)
        return acknowledgment(key, event)

    def _start_ready(self, key):
        self._scheduler.advance(key)
        if not self._stopping and key in self._discovered:
            legacy.start_ready(self, key)

    def _initiate(self, key, action_id):
        try:
            self._runner.initiate(*key, copy.deepcopy(self._states[key]["actions"][action_id]))
        except Exception as error:
            if self._unavailable or self._closed:
                raise
            LOGGER.exception("Could not initiate action %s for %s/%s", action_id, *key)
            if self._states[key]["actions"][action_id]["status"] != "completed":
                self._commit_completion(
                    key,
                    {
                        "request_id": f"engine:failure:{action_id}",
                        "action_id": action_id,
                        "type": "completed",
                        "outcome": "failed",
                        "result": None,
                        "error": f"Action initiation failed: {error}",
                    },
                )

    def _execute_sync(self, key, action):
        from .runner import action_configuration

        def execute(_):
            raise RuntimeError("Synchronous handlers cannot launch external commands.")

        try:
            config = action_configuration(self, key, action)
            result = run_action(config, execute, Path(config["job_path"]) / "output.log")
            # Reject malformed results before appending a completion event.
            canonical_json(result)
            return {"outcome": "succeeded", "result": result, "error": None}
        except Exception as error:
            return {"outcome": "failed", "result": None, "error": str(error)}

    def _commit_completion(self, key, request):
        action = self._states[key]["actions"].get(request["action_id"])
        if action is not None and action.get("command_id"):
            return self._scheduler.complete(key, request)
        return legacy.complete_action(self, key, request)

    def command(self, task_set, task, body):
        if self._stopping:
            raise EngineError("Engine is stopping.", status=503, code="engine_unavailable")
        return self._scheduler.accept(task_set, task, body)

    def cancel(self, task_set, task, body):
        return self._scheduler.cancel(task_set, task, body)
