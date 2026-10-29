"""Launch detached workers and reconcile their durable completions after restart."""

from __future__ import annotations

import codecs
import copy
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, overload

from engine.type_defs import (
    ActionConfig,
    ActionState,
    CapturedAction,
    Completion,
    JsonObject,
    JsonValue,
    Outcome,
    OutputResponse,
    PathLike,
    TaskKey,
)

from .actions import reject_constant
from .actions.recovery import validate_failure
from .errors import EngineError
from .storage import atomic_json_write, sync_directory
from .worker import (
    owned_group,
    process_identity,
    same_process,
    stop_group,
    tagged_groups,
)

if TYPE_CHECKING:
    from engine.core import Engine

LOGGER = logging.getLogger(__name__)
DIRECTORY = Path(__file__).resolve().parent


def job_directory(
    root: PathLike, key: TaskKey, action_id: str, *, create: bool = False
) -> Path:
    directory = Path(root)
    for component in (*key, ".review", "jobs", "engine", action_id):
        directory = directory / component
        if directory.is_symlink():
            raise EngineError("Execution artifacts must not be symlinks.")
        if create and not directory.exists():
            directory.mkdir(mode=0o700, exist_ok=True)
            sync_directory(directory.parent)
    return directory


@overload
def read_json(file: Path, default: JsonObject) -> JsonObject: ...


@overload
def read_json(file: Path, default: None = None) -> JsonObject | None: ...


def read_json(file: Path, default: JsonObject | None = None) -> JsonObject | None:
    if file.is_symlink():
        raise EngineError("Execution artifacts must not be symlinks.")
    try:
        value = cast(
            JsonValue,
            json.loads(file.read_text("utf-8"), parse_constant=reject_constant),
        )
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def read_output(
    root: PathLike, key: TaskKey, action_id: str, after: int, *, final: bool = False
) -> OutputResponse:
    file = job_directory(root, key, action_id) / "output.log"
    if file.is_symlink():
        raise EngineError("Execution output must not be a symlink.")
    if not file.exists():
        return {"action_id": action_id, "after": after, "next": after, "output": ""}
    with file.open("rb") as stream:
        stream.seek(after)
        data = stream.read(256 * 1024)
    # Preserve a partial UTF-8 character for the next byte-offset request.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(data, final=final and len(data) < 256 * 1024)
    tail = decoder.getstate()[0]
    if tail:
        data = data[: -len(tail)]
    return {
        "action_id": action_id,
        "after": after,
        "next": after + len(data),
        "output": text,
    }


def action_configuration(
    engine: Engine, key: TaskKey, action: ActionState
) -> ActionConfig:
    root = Path(engine.catalog["tasks_root"])
    # Historical input retains its complete action record for replay compatibility.
    captured: CapturedAction = cast(CapturedAction, copy.deepcopy(action))
    if action.get("command_id"):
        captured = {
            "action_id": action["action_id"],
            "name": action["name"],
            "parameters": copy.deepcopy(action["parameters"]),
            "task_set": key[0],
            "task": key[1],
        }
    captured.update({"task_set": key[0], "task": key[1]})
    execution = action["execution"]
    assert execution is not None
    config: ActionConfig = {
        "schema_version": 2 if action.get("command_id") else 1,
        "action": captured,
        "execution": copy.deepcopy(execution),
        "task_path": str(root.joinpath(*key)),
        "job_path": str(job_directory(root, key, action["action_id"])),
    }
    if not action.get("command_id"):
        from .commands.legacy import capture_results

        config = capture_results(config, engine._states[key])
    return config


class ActionRunner:
    def __init__(self: Self, engine: Engine) -> None:
        self.engine = engine
        self.root = Path(engine.catalog["tasks_root"])
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.children: list[subprocess.Popen[bytes]] = []
        from .git_worker import GitWorker

        self.git_worker = GitWorker(self)

    def initiate(self: Self, task_set: str, task: str, action: ActionState) -> None:
        if not action.get("execution"):
            raise RuntimeError(
                "No execution specification was captured; submit a new command."
            )
        key = (task_set, task)
        directory = job_directory(self.root, key, action["action_id"], create=True)
        config = action_configuration(self.engine, key, action)
        from .git_worker import uses_git_worker

        if uses_git_worker(action):
            self.git_worker.enqueue(key, action, config)
            return
        atomic_json_write(directory / "input.json", config)
        child = subprocess.Popen(
            [
                sys.executable,
                str(DIRECTORY / "worker.py"),
                str(directory / "input.json"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=DIRECTORY.parent,
            start_new_session=True,
        )
        # The parent writes an identity immediately, closing the startup/worker.json gap.
        self.children.append(child)
        try:
            atomic_json_write(
                directory / "launch.json",
                {"pid": child.pid, "identity": process_identity(child.pid)},
            )
        except BaseException:
            # A launch receipt failure cannot leave an untracked action running.
            child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            for pgid in tagged_groups(action["action_id"]):
                stop_group(pgid, 0.2)
            raise

    def cancel(self: Self, key: TaskKey, action_id: str) -> None:
        from .git_worker import uses_git_worker

        if uses_git_worker(self.engine._states[key]["actions"][action_id]):
            self.git_worker.cancel(key, action_id)
            return
        directory = job_directory(self.root, key, action_id, create=True)
        atomic_json_write(directory / "cancel.json", {"cancel": True})

    def _completion(self: Self, key: TaskKey, action: ActionState) -> Completion | None:
        directory = job_directory(self.root, key, action["action_id"])
        result = read_json(directory / "result.json")
        if (
            isinstance(result, dict)
            and isinstance(result.get("outcome"), str)
            and result.get("outcome")
            in {"succeeded", "failed", "cancelled", "timed_out"}
            and (result.get("error") is None or isinstance(result.get("error"), str))
        ):
            completion: Completion = {
                "outcome": cast(Outcome, result["outcome"]),
                "result": result.get("result"),
                "error": cast(str | None, result.get("error")),
            }
            if "failure" in result:
                completion["failure"] = validate_failure(result["failure"])
            return completion
        from .git_worker import uses_git_worker

        if uses_git_worker(action):
            return None
        worker = read_json(directory / "worker.json", {}) or read_json(
            directory / "launch.json", {}
        )
        if same_process(worker.get("pid"), worker.get("identity")):
            if action.get("cancel_requested"):
                self.cancel(key, action["action_id"])
            return None
        # A dead worker must not leave commands behind. Saved process identity
        # and stable job tags avoid signaling an unrelated reused PID/group.
        process = read_json(directory / "process.json", {})
        if same_process(process.get("pid"), process.get("identity")) or owned_group(
            process.get("pgid"), action["action_id"]
        ):
            pid = process["pid"]
            assert isinstance(pid, int)
            stop_group(pid, 0.2)
        for pgid in tagged_groups(action["action_id"]):
            stop_group(pgid, 0.2)
        return {
            "outcome": "failed",
            "result": None,
            "error": "Execution worker was interrupted. Submit a new command to retry; the action was not relaunched.",
            "failure": {
                "classification": "unknown",
                "origin": "worker",
                "diagnostics": "Execution worker was interrupted.",
                "exit_code": None,
            },
        }

    def recover(self: Self) -> None:
        self.git_worker.recover()
        for key, state in list(self.engine._states.items()):
            for action in list(state["actions"].values()):
                if action["status"] == "started":
                    completion = self._completion(key, action)
                    if completion is not None:
                        self._apply(key, action["action_id"], completion)

    def _apply(self: Self, key: TaskKey, action_id: str, result: Completion) -> None:
        action = self.engine._states[key]["actions"][action_id]
        if action["status"] == "completed":
            return
        self.engine._commit_completion(
            key,
            {
                "request_id": f"engine:completion:{action_id}",
                "action_id": action_id,
                "type": "completed",
                "outcome": result["outcome"],
                "result": result["result"],
                "error": result["error"],
                "failure": result.get("failure"),
            },
        )
        self.engine._start_ready(key)

    def _monitor(self: Self) -> None:
        while not self.stop.wait(0.2):
            try:
                with self.engine._lock:
                    if self.engine._closed or self.engine._unavailable:
                        continue
                    for key, state in list(self.engine._states.items()):
                        for action in list(state["actions"].values()):
                            if action["status"] != "started":
                                continue
                            completion = self._completion(key, action)
                            if completion is not None:
                                self._apply(key, action["action_id"], completion)
                        # A delayed head stays in the durable pool until due.
                        self.engine._start_ready(key)
                self.children[:] = [
                    child for child in self.children if child.poll() is None
                ]
            except Exception:
                LOGGER.exception("Could not reconcile an execution result; retrying")

    def start_monitor(self: Self) -> None:
        self.git_worker.start()
        self.thread = threading.Thread(
            target=self._monitor, name="engine-executions", daemon=True
        )
        self.thread.start()

    def close(self: Self) -> None:
        from .git_worker import uses_git_worker

        self.git_worker.close()
        self.stop.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join()
        with self.engine._lock:
            if not self.engine._unavailable:
                for key, state in list(self.engine._states.items()):
                    for action in list(state["actions"].values()):
                        if action["status"] == "started" and uses_git_worker(action):
                            completion = self._completion(key, action)
                            if completion is not None:
                                self._apply(key, action["action_id"], completion)
        for child in self.children:
            child.poll()
        # Workers are deliberately detached and finish while the server is offline.
