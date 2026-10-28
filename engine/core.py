import copy
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .actions import canonical_json, fingerprint, load_actions, validate_name, validate_request_id
from .discovery import discover_tasks, validate_component
from .errors import CorruptLogError, EngineError
from .state import OUTCOMES, dependency_references, empty_state, reduce_event
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
    return {
        "task_set": key[0],
        "task": key[1],
        "action_id": event["action_id"],
        "event_id": event["event_id"],
        "sequence": event["sequence"],
        "type": event["type"],
    }


class Engine:
    def __init__(
        self,
        tasks_root=DEFAULT_TASKS_ROOT,
        *,
        actions_file=None,
        initiate_action=None,
        execute=False,
    ):
        self._lock = threading.RLock()
        self._closed = False
        self._unavailable = False
        self._store = None
        self._runner = None
        if execute and initiate_action is not None:
            raise EngineError("Choose execute or initiate_action, not both.")
        self._initiate_action = self._log_initiation if initiate_action is None else initiate_action
        if not callable(self._initiate_action):
            raise EngineError("initiate_action must be callable.")
        self.catalog, self._discovered = discover_tasks(tasks_root)
        self.actions = load_actions(actions_file or ENGINE_DIRECTORY / "actions.json")
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
            if execute:
                from .runner import ActionRunner

                self._runner = ActionRunner(self)
                self._initiate_action = self._runner.initiate
                self._runner.recover()
            # Resume commands left ready by a crash, without relaunching started actions.
            with self._lock:
                for key in sorted(self._discovered):
                    self._start_ready(key)
            if self._runner is not None:
                self._runner.start_monitor()
        except BaseException:
            if self._runner is not None:
                self._runner.close()
            if self._store is not None:
                self._store.close()
            raise

    def close(self):
        with self._lock:
            self._closed = True
        if self._runner is not None:
            self._runner.close()
        with self._lock:
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
        with self._lock:
            return {"schema_version": 1, "actions": copy.deepcopy(self.actions)}

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
        try:
            self._store.write_snapshot(key, self._states[key])
        except OSError:
            # The durable event remains authoritative; a later write or restart rebuilds this cache.
            LOGGER.exception("Could not update job.json for %s/%s", *key)

    def _commit(self, key, request, operation, event_type, action_id, payload):
        event = {
            "schema_version": 1,
            "sequence": self._states[key]["revision"] + 1,
            "event_id": str(uuid.uuid4()),
            "request_id": request["request_id"],
            "request_fingerprint": fingerprint(operation, request),
            "action_id": action_id,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": event_type,
            "payload": payload,
        }
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

    @staticmethod
    def _log_initiation(task_set, task, action):
        # Demo actions have no process runner yet. A runner replaces this hook.
        LOGGER.info(
            "Initiated %s/%s action %s (%s); awaiting completion signal",
            task_set,
            task,
            action["name"],
            action["action_id"],
        )

    def _start_ready(self, key):
        """Persist each start before handing it to the execution integration."""
        while self._states[key]["ready_action_ids"]:
            self._check_available()
            action_id = self._states[key]["ready_action_ids"][0]
            action = self._states[key]["actions"][action_id]
            self._commit(
                key,
                {
                    "request_id": f"engine:start:{action_id}",
                    "action_id": action_id,
                    "type": "started",
                },
                "start",
                "started",
                action_id,
                {"dependency_actions": dependency_references(self._states[key], action)},
            )
            try:
                self._initiate_action(*key, copy.deepcopy(self._states[key]["actions"][action_id]))
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

    def _commit_completion(self, key, request):
        self._check_available()
        action = self._states[key]["actions"].get(request["action_id"])
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
        return self._commit(
            key,
            request,
            "report",
            "completed",
            request["action_id"],
            {
                "dependency_actions": copy.deepcopy(action["dependency_actions"]),
                **{name: request[name] for name in ("outcome", "result", "error")},
            },
        )

    def command(self, task_set, task, body):
        validate_body(body, ("request_id", "action"), ("parameters",))
        validate_name(body["action"])
        if not isinstance(body.get("parameters", {}), dict):
            raise EngineError("parameters must be a JSON object.")
        request = {
            "request_id": body["request_id"],
            "action": body["action"],
            "parameters": copy.deepcopy(body.get("parameters", {})),
        }
        with self._lock:
            self._check_available()
            key = self._key(task_set, task)
            previous = self._duplicate(key, request["request_id"], fingerprint("command", request))
            if previous is not None:
                return previous
            self._ensure_discovered(key)
            definition = self.actions.get(request["action"])
            if definition is None:
                raise EngineError("Unknown predefined action.", status=404, code="action_not_found")
            action_id = str(uuid.uuid4())
            extra = {}
            if "execution_type" in definition:
                extra["execution_type"] = definition["execution_type"]
            if "runner" in definition:
                from .workflow import prepare_execution

                extra["execution"] = prepare_execution(definition["runner"], request["parameters"])
            elif self._runner is not None:
                raise EngineError(
                    "This action has no executable runner; use --manual or configure runner."
                )
            accepted = self._commit(
                key,
                request,
                "command",
                "waiting",
                action_id,
                {
                    "action": request["action"],
                    "parameters": request["parameters"],
                    "dependencies": copy.deepcopy(definition["dependencies"]),
                    **extra,
                },
            )
            self._start_ready(key)
            return accepted

    def complete(self, task_set, task, body):
        validate_body(body, ("request_id", "action_id", "outcome"), ("type", "result", "error"))
        if body.get("type", "completed") != "completed":
            raise EngineError(
                "Only completion signals are accepted; the server owns action starts."
            )
        if not isinstance(body["outcome"], str) or body["outcome"] not in OUTCOMES:
            raise EngineError("outcome must be succeeded, failed, cancelled or timed_out.")
        if body.get("error") is not None and not isinstance(body["error"], str):
            raise EngineError("error must be a string or null.")
        request = {
            "request_id": body["request_id"],
            "action_id": body["action_id"],
            "type": "completed",
            "outcome": body["outcome"],
            "result": copy.deepcopy(body.get("result")),
            "error": body.get("error"),
        }
        if not isinstance(request["action_id"], str):
            raise EngineError("action_id must be a UUID string.")
        with self._lock:
            self._check_available()
            key = self._key(task_set, task)
            previous = self._duplicate(key, request["request_id"], fingerprint("report", request))
            if previous is not None:
                return previous
            self._ensure_discovered(key)
            action = self._states[key]["actions"].get(request["action_id"])
            if self._runner is not None and action and action.get("execution"):
                raise EngineError(
                    "The execution worker owns this action's completion.",
                    status=409,
                    code="worker_owned_action",
                )
            completed = self._commit_completion(key, request)
            self._start_ready(key)
            return completed

    def cancel(self, task_set, task, body):
        validate_body(body, ("request_id", "action_id"))
        if not isinstance(body["action_id"], str):
            raise EngineError("action_id must be a UUID string.")
        request = copy.deepcopy(body)
        with self._lock:
            self._check_available()
            key = self._key(task_set, task)
            previous = self._duplicate(key, request["request_id"], fingerprint("cancel", request))
            if previous is not None:
                return previous
            action = self._states[key]["actions"].get(request["action_id"])
            if action is None:
                raise EngineError("Unknown action ID.", status=404, code="action_not_found")
            accepted = self._commit(
                key, request, "cancel", "cancel_requested", request["action_id"], {}
            )
            if action["status"] == "started":
                if self._runner is not None and action.get("execution"):
                    self._runner.cancel(key, request["action_id"])
                else:
                    self._commit_completion(
                        key,
                        {
                            "request_id": f"engine:cancel:{request['action_id']}",
                            "action_id": request["action_id"],
                            "type": "completed",
                            "outcome": "cancelled",
                            "result": None,
                            "error": None,
                        },
                    )
            self._start_ready(key)
            return accepted
