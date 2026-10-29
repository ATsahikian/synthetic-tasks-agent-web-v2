from __future__ import annotations

import fcntl
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Self, cast

from engine.type_defs import Event, PathLike, State, TaskKey

from .actions import reject_constant
from .discovery import child_directories, validate_component
from .errors import CorruptLogError, EngineError

LOGGER = logging.getLogger(__name__)


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json_write(file: Path, value: object) -> None:
    temporary = file.with_name(f".{file.name}.tmp-{uuid.uuid4()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, file)
        sync_directory(file.parent)
    finally:
        temporary.unlink(missing_ok=True)


class EventStore:
    """Task histories and locks live in task .review folders."""

    def __init__(self: Self, tasks_root: PathLike) -> None:
        self.directory = Path(tasks_root).resolve()
        self._lock_descriptors: dict[TaskKey, int] = {}
        if not self.directory.is_dir():
            raise EngineError(
                f"Task root must be an existing directory: {self.directory}"
            )

    def close(self: Self) -> None:
        for descriptor in self._lock_descriptors.values():
            os.close(descriptor)
        self._lock_descriptors.clear()

    def _set_directory(self: Self, task_set: str) -> Path:
        validate_component(task_set)
        directory = self.directory / task_set
        if directory.is_symlink() or not directory.is_dir():
            raise EngineError(
                f"Task-set directory must exist and not be a symlink: {directory}"
            )
        return directory

    @staticmethod
    def _review_directory(directory: Path, *, create: bool = False) -> Path:
        review = directory / ".review"
        if review.is_symlink():
            raise EngineError(f"Review directories must not be symlinks: {review}")
        if create and not review.exists():
            review.mkdir(mode=0o700)
            sync_directory(review)
            sync_directory(directory)
        if review.exists() and not review.is_dir():
            raise EngineError(f"Review path must be a directory: {review}")
        return review

    def ensure_workspace(self: Self, task_set: str) -> None:
        review = self._review_directory(self._set_directory(task_set), create=True)
        file = review / "workspace.json"
        if file.is_symlink():
            raise EngineError("Workspace metadata must not be a symlink.")
        manifest = {
            "schema_version": 1,
            "tasks_root": str(self.directory),
            "task_set": task_set,
        }
        # Location determines ownership. Refresh these derived paths if a project moves.
        saved = None
        if file.exists():
            try:
                saved = json.loads(
                    file.read_text("utf-8"), parse_constant=reject_constant
                )
            except ValueError:
                pass
        if saved != manifest:
            atomic_json_write(file, manifest)

    def _task_directory(self: Self, key: TaskKey, *, create: bool = False) -> Path:
        task_set, task = key
        validate_component(task)
        directory = self._set_directory(task_set) / task
        if directory.is_symlink() or not directory.is_dir():
            raise EngineError(
                f"Task directory must exist and not be a symlink: {directory}"
            )
        return self._review_directory(directory, create=create)

    def task_keys(self: Self) -> set[TaskKey]:
        result = set()
        for set_directory in child_directories(self.directory):
            for task_directory in child_directories(set_directory):
                if (task_directory / ".review" / "events.jsonl").exists():
                    key = (set_directory.name, task_directory.name)
                    self._task_directory(key)
                    result.add(key)
        return result

    def ensure_task(self: Self, key: TaskKey) -> None:
        directory = self._task_directory(key, create=True)
        if key not in self._lock_descriptors:
            descriptor = os.open(
                directory / "engine.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            self._lock_descriptors[key] = descriptor
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise EngineError(
                    f"Another engine owns task {key[0]}/{key[1]}.",
                    status=409,
                    code="engine_already_running",
                ) from error
        file = directory / "events.jsonl"
        descriptor = os.open(
            file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        os.close(descriptor)
        sync_directory(file.parent)

    def read_events(self: Self, key: TaskKey) -> list[Event]:
        file = self._task_directory(key) / "events.jsonl"
        if file.is_symlink():
            raise CorruptLogError(f"Event log must not be a symlink: {file}")
        if not file.exists():
            return []
        with file.open("r+b") as handle:
            data = handle.read()
            boundary = data.rfind(b"\n") + 1
            if boundary != len(data):
                LOGGER.warning("Recovering incomplete trailing event in %s", file)
                handle.truncate(boundary)
                handle.flush()
                os.fsync(handle.fileno())
                data = data[:boundary]
        events = []
        for index, line in enumerate(data.split(b"\n")[:-1], 1):
            try:
                event = json.loads(line.decode("utf-8"), parse_constant=reject_constant)
            except (UnicodeError, ValueError) as error:
                raise CorruptLogError(
                    f"Invalid event at {file}:{index}: {error}"
                ) from error
            events.append(cast(Event, event))
        return events

    def append(self: Self, key: TaskKey, event: Event) -> None:
        file = self._task_directory(key) / "events.jsonl"
        encoded = (
            json.dumps(
                event, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            remaining = memoryview(encoded)
            while remaining:
                count = os.write(descriptor, remaining)
                if count == 0:
                    raise OSError("Event write made no progress.")
                remaining = remaining[count:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def write_snapshot(self: Self, key: TaskKey, state: State) -> None:
        atomic_json_write(self._task_directory(key) / "job.json", state)

    def write_pool(self: Self, key: TaskKey, state: State) -> None:
        file = self._task_directory(key) / "action-pool.json"
        if file.is_symlink():
            raise EngineError("Action pool must not be a symlink.")
        atomic_json_write(file, state["action_pool"])
