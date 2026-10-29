"""One durable FIFO executor for every engine-owned Git transaction."""

from __future__ import annotations

import copy
import fcntl
import json
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

from engine.type_defs import (
    ActionConfig,
    ActionState,
    Completion,
    QueueEntry,
    QueueState,
    TaskKey,
)

from ..actions import canonical_json, run_action
from ..errors import CorruptLogError, EngineError
from ..storage import atomic_json_write, sync_directory
from ..worker import Executor, stop_group, tagged_groups

if TYPE_CHECKING:
    from engine.runner import ActionRunner

LOGGER = logging.getLogger(__name__)


def uses_git_worker(action: ActionState) -> bool:
    return (action.get("execution") or {}).get("executor") == "git-worker"


class GitWorker:
    def __init__(self: Self, runner: ActionRunner) -> None:
        self.runner = runner
        self.engine = runner.engine
        review = runner.root / ".review"
        if review.is_symlink():
            raise CorruptLogError("Git queue directory must not be a symlink.")
        review.mkdir(mode=0o700, exist_ok=True)
        sync_directory(review.parent)
        self.file = review / "git-queue.json"
        self.owner: int | None = os.open(
            review / "git-worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.state = self._read()
        except BaseException:
            os.close(self.owner)
            self.owner = None
            raise
        self.condition = threading.Condition()
        self.closing = False
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def _read(self: Self) -> QueueState:
        if self.file.is_symlink():
            raise CorruptLogError("Git queue must not be a symlink.")
        if not self.file.exists():
            return {"schema_version": 1, "next_sequence": 1, "requests": []}
        try:
            state = cast(QueueState, json.loads(self.file.read_text("utf-8")))
            if type(state["schema_version"]) is not int or state["schema_version"] != 1:
                raise ValueError("Unsupported queue schema")
            if type(state["next_sequence"]) is not int or state["next_sequence"] <= 0:
                raise ValueError("Invalid queue sequence")
            seen, sequences = set(), []
            for entry in state["requests"]:
                identity = (entry["task_set"], entry["task"], entry["action_id"])
                action = self.engine._states[(identity[0], identity[1])]["actions"][
                    identity[2]
                ]
                if not uses_git_worker(action) or identity in seen:
                    raise ValueError("Invalid queue action")
                if (
                    type(entry["sequence"]) is not int
                    or not 0 < entry["sequence"] < state["next_sequence"]
                ):
                    raise ValueError("Invalid queue entry sequence")
                if entry["phase"] not in {"queued", "running", "cancelled", "done"}:
                    raise ValueError("Invalid queue phase")
                seen.add(identity)
                sequences.append(entry["sequence"])
            if sequences != sorted(set(sequences)):
                raise ValueError("Queue order is inconsistent")
            return state
        except (ValueError, KeyError, TypeError) as error:
            raise CorruptLogError(
                "Cannot read durable Git queue; preserve it for recovery."
            ) from error

    def _save(self: Self) -> None:
        try:
            atomic_json_write(self.file, self.state)
        except OSError as error:
            self.engine._unavailable = True
            raise EngineError(
                "Git queue persistence failed. Restart and retry with the same request_id.",
                status=503,
                code="git_persistence_failed",
            ) from error

    def enqueue(
        self: Self, key: TaskKey, action: ActionState, config: ActionConfig
    ) -> None:
        from ..runner import job_directory

        with self.condition:
            if self.closing or self.error:
                raise RuntimeError("Git worker is stopping.")
            identity = (*key, action["action_id"])
            if any(
                (e["task_set"], e["task"], e["action_id"]) == identity
                for e in self.state["requests"]
            ):
                return
            directory = job_directory(
                self.runner.root, key, action["action_id"], create=True
            )
            if not (directory / "input.json").exists():
                try:
                    atomic_json_write(directory / "input.json", config)
                except OSError as error:
                    self.engine._unavailable = True
                    raise EngineError(
                        "Git input persistence failed. Restart and retry with the same request_id.",
                        status=503,
                        code="git_persistence_failed",
                    ) from error
            self.state["requests"] = [
                e for e in self.state["requests"] if e["phase"] != "done"
            ]
            self.state["requests"].append(
                {
                    "sequence": self.state["next_sequence"],
                    "task_set": key[0],
                    "task": key[1],
                    "action_id": action["action_id"],
                    "phase": "queued",
                }
            )
            self.state["next_sequence"] += 1
            self._save()
            self.condition.notify()

    def recover(self: Self) -> None:
        from ..runner import action_configuration, job_directory

        # Starts without an enqueue record can only precede new admissions.
        missing = []
        for key, state in self.engine._states.items():
            for action in state["actions"].values():
                if action["status"] == "started" and uses_git_worker(action):
                    missing.append((action["started_at"], key, action))
        for _, key, action in sorted(
            missing, key=lambda item: (item[0], item[1], item[2]["action_id"])
        ):
            self.enqueue(key, action, action_configuration(self.engine, key, action))
        for entry in self.state["requests"]:
            key = (entry["task_set"], entry["task"])
            action = self.engine._states[key]["actions"][entry["action_id"]]
            if action["status"] == "completed":
                entry["phase"] = "done"
            else:
                directory = job_directory(self.runner.root, key, action["action_id"])
                if (directory / "result.json").exists():
                    self._saved_result(directory)
                elif entry["phase"] == "done":
                    raise CorruptLogError(
                        "Completed Git queue entry has no durable result."
                    )
                if action.get("cancel_requested") and entry["phase"] == "queued":
                    entry["phase"] = "cancelled"
        self._save()

    def cancel(self: Self, key: TaskKey, action_id: str) -> None:
        with self.condition:
            for entry in self.state["requests"]:
                if (entry["task_set"], entry["task"], entry["action_id"]) == (
                    *key,
                    action_id,
                ):
                    if entry["phase"] == "queued":
                        entry["phase"] = "cancelled"
                        self._save()
                        self.condition.notify()
                    return

    def start(self: Self) -> None:
        self.thread = threading.Thread(
            target=self._run, name="git-worker", daemon=False
        )
        self.thread.start()

    def _run(self: Self) -> None:
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(
                        lambda: (
                            self.closing
                            or any(e["phase"] != "done" for e in self.state["requests"])
                        )
                    )
                    if self.engine._unavailable:
                        return
                    entry = next(
                        (e for e in self.state["requests"] if e["phase"] != "done"),
                        None,
                    )
                    if entry is None:
                        return
                    cancelled = entry["phase"] == "cancelled"
                    recovering = entry["phase"] == "running"
                    if not cancelled:
                        entry["phase"] = "running"
                    self._save()
                    captured = copy.deepcopy(entry)
                    captured["recovering"] = recovering
                self._execute(captured, cancelled)
                with self.condition:
                    entry["phase"] = "done"
                    self._save()
        except BaseException as error:
            self.error = error
            self.engine._unavailable = True
            LOGGER.exception(
                "Git worker stopped; durable requests will recover after restart"
            )

    def _execute(self: Self, entry: QueueEntry, cancelled: bool) -> None:
        from ..runner import job_directory, read_json

        key = (entry["task_set"], entry["task"])
        directory = job_directory(self.runner.root, key, entry["action_id"])
        if (directory / "result.json").exists():
            self._saved_result(directory)
            return
        # An interrupted supervised Git command may outlive the previous server.
        if entry.get("recovering"):
            for group in tagged_groups(entry["action_id"]):
                stop_group(group, 0.2)
        config = read_json(directory / "input.json")
        if config is None:
            raise CorruptLogError("Missing or malformed durable Git action input.")
        with (directory / "output.log").open("ab", buffering=0) as output:
            executor = Executor(
                cast(ActionConfig, config), directory, output, cancellable=False
            )
            try:
                if cancelled:
                    completion: Completion = {
                        "outcome": "cancelled",
                        "result": None,
                        "error": "Git request cancelled before execution.",
                    }
                else:
                    result = run_action(
                        cast(ActionConfig, config),
                        executor.execute,
                        directory / "output.log",
                    )
                    canonical_json(result)
                    completion = {
                        "outcome": "succeeded",
                        "result": result,
                        "error": None,
                    }
            except Exception as error:
                output.write(
                    f"[git-worker] {error}\n".encode("utf-8", errors="replace")
                )
                completion = {
                    "outcome": executor.outcome or "failed",
                    "result": None,
                    "error": str(error),
                }
            output.flush()
            os.fsync(output.fileno())
            atomic_json_write(directory / "result.json", completion)

    def _saved_result(self: Self, directory: Path) -> Completion:
        from ..runner import read_json

        result = read_json(directory / "result.json")
        if (
            result is None
            or set(result) != {"outcome", "result", "error"}
            or not isinstance(result.get("outcome"), str)
            or result.get("outcome")
            not in {"succeeded", "failed", "cancelled", "timed_out"}
            or (
                result.get("error") is not None and not isinstance(result["error"], str)
            )
        ):
            raise CorruptLogError(
                "Malformed durable Git result; preserve it for recovery."
            )
        return cast(Completion, result)

    def close(self: Self) -> None:
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join()
        if self.owner is not None:
            os.close(self.owner)
            self.owner = None
