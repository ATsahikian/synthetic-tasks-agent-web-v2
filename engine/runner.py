"""Launch detached workers and reconcile their durable completions after restart."""

import codecs
import copy
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

from .actions import reject_constant
from .errors import EngineError
from .storage import atomic_json_write, sync_directory
from .worker import owned_group, process_identity, same_process, stop_group, tagged_groups

LOGGER = logging.getLogger(__name__)
DIRECTORY = Path(__file__).resolve().parent


def job_directory(root, key, action_id, *, create=False):
    directory = Path(root)
    for component in (*key, ".review", "jobs", "engine", action_id):
        directory = directory / component
        if directory.is_symlink():
            raise EngineError("Execution artifacts must not be symlinks.")
        if create and not directory.exists():
            directory.mkdir(mode=0o700)
            sync_directory(directory.parent)
    return directory


def read_json(file, default=None):
    if file.is_symlink():
        raise EngineError("Execution artifacts must not be symlinks.")
    try:
        value = json.loads(file.read_text("utf-8"), parse_constant=reject_constant)
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def read_output(root, key, action_id, after, *, final=False):
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
    return {"action_id": action_id, "after": after, "next": after + len(data), "output": text}


class ActionRunner:
    def __init__(self, engine):
        self.engine = engine
        self.root = Path(engine.catalog["tasks_root"])
        self.stop = threading.Event()
        self.thread = None
        self.children = []

    def initiate(self, task_set, task, action):
        if not action.get("execution"):
            raise RuntimeError("No execution specification was captured; submit a new command.")
        key = (task_set, task)
        directory = job_directory(self.root, key, action["action_id"], create=True)
        captured = copy.deepcopy(action)
        captured.update({"task_set": task_set, "task": task})
        config = {
            "schema_version": 1,
            "action": captured,
            "execution": action["execution"],
            "task_path": str(self.root.joinpath(*key)),
            "job_path": str(directory),
            "dependency_results": {
                name: copy.deepcopy(self.engine._states[key]["actions"][action_id]["result"])
                for name, action_id in action["dependency_actions"].items()
            },
        }
        atomic_json_write(directory / "input.json", config)
        child = subprocess.Popen(
            [sys.executable, str(DIRECTORY / "worker.py"), str(directory / "input.json")],
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

    def cancel(self, key, action_id):
        directory = job_directory(self.root, key, action_id, create=True)
        atomic_json_write(directory / "cancel.json", {"cancel": True})

    def _completion(self, key, action):
        directory = job_directory(self.root, key, action["action_id"])
        result = read_json(directory / "result.json")
        if (
            isinstance(result, dict)
            and isinstance(result.get("outcome"), str)
            and result.get("outcome") in {"succeeded", "failed", "cancelled", "timed_out"}
            and (result.get("error") is None or isinstance(result.get("error"), str))
        ):
            return result
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
            stop_group(process["pid"], 0.2)
        for pgid in tagged_groups(action["action_id"]):
            stop_group(pgid, 0.2)
        return {
            "outcome": "failed",
            "result": None,
            "error": "Execution worker was interrupted. Submit a new command to retry; the action was not relaunched.",
        }

    def recover(self):
        for key, state in list(self.engine._states.items()):
            for action in list(state["actions"].values()):
                if action["status"] == "started":
                    completion = self._completion(key, action)
                    if completion is not None:
                        self._apply(key, action["action_id"], completion)

    def _apply(self, key, action_id, result):
        action = self.engine._states[key]["actions"][action_id]
        if action["status"] == "completed":
            return
        result = {key: result.get(key) for key in ("outcome", "result", "error")}
        self.engine._commit_completion(
            key,
            {
                "request_id": f"engine:completion:{action_id}",
                "action_id": action_id,
                "type": "completed",
                **result,
            },
        )
        if key in self.engine._discovered:
            self.engine._start_ready(key)

    def _monitor(self):
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
                self.children[:] = [child for child in self.children if child.poll() is None]
            except Exception:
                LOGGER.exception("Could not reconcile an execution result; retrying")

    def start_monitor(self):
        self.thread = threading.Thread(target=self._monitor, name="engine-executions", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join()
        for child in self.children:
            child.poll()
        # Workers are deliberately detached and finish while the server is offline.
