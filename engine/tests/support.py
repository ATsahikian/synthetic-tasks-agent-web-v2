from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import ModuleType
from typing import (
    Callable,
    Mapping,
    Required,
    Self,
    Sequence,
    TypedDict,
    TypeVar,
    Unpack,
    cast,
)
from unittest import mock

from engine.actions import fingerprint, prepare_execution, validate_name
from engine.actions.base import ActionHandler
from engine.commands.legacy import dependency_references
from engine.core import Engine, acknowledgment, validate_body
from engine.errors import EngineError
from engine.runner import ActionRunner, job_directory
from engine.storage import atomic_json_write
from engine.type_defs import (
    ActionConfig,
    ActionDefinition,
    ActionState,
    CommandDefinition,
    EventPayload,
    ExecutionCommand,
    ExecutionSpec,
    FailureInfo,
    JsonObject,
    JsonValue,
    Outcome,
    Receipt,
    RequestBody,
)
from engine.worker import same_process, stop_group

T = TypeVar("T")


class EngineOptions(TypedDict, total=False):
    commands: dict[str, CommandDefinition] | None


class RunnerOptions(TypedDict, total=False):
    workers: bool
    initiate: Callable[[str, str, ActionState], None] | None


class ActionReceipt(TypedDict, total=False):
    task_set: Required[str]
    task: Required[str]
    action_id: Required[str]
    event_id: Required[str]
    sequence: Required[int]
    type: Required[str]
    command_id: str
    action_ids: list[str]
    outcome: Outcome
    result: JsonValue
    error: str | None
    failed_action_id: str | None


def action_receipt(receipt: Receipt) -> ActionReceipt:
    assert isinstance(receipt["action_id"], str)
    return cast(ActionReceipt, receipt)


class action_test_approval(ActionHandler):
    """A test-only worker handler with the approval parameter contract."""

    name = "approve"
    parameters = frozenset({"source"})
    required_parameters = frozenset({"source"})

    def run(
        self: Self,
        config: ActionConfig,
        execute: Callable[[ExecutionCommand], int],
        output_path: Path,
    ) -> JsonObject:
        return {"approved": True}


def install_approval_handler(test: unittest.TestCase) -> None:
    module = ModuleType("engine.actions.action_test_approval")
    setattr(module, "action_test_approval", action_test_approval)
    registration = mock.patch.dict(sys.modules, {module.__name__: module})
    registration.start()
    test.addCleanup(registration.stop)


def git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()


def task_repository(root: Path) -> None:
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Engine fixture")
    git(root, "config", "user.email", "engine@example.test")
    git(root, "config", "core.hooksPath", str(root / ".git" / "hooks"))
    (root / ".gitignore").write_text("**/.review/\n")
    git(root, "add", "--", ".gitignore")
    git(root, "commit", "--no-gpg-sign", "-m", "Initial task project")


def export_repository(base: Path) -> tuple[Path, Path]:
    remote, checkout = base / "remote.git", base / "cyber-bench"
    remote.mkdir()
    checkout.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(checkout, "init", "--initial-branch=main")
    git(checkout, "config", "user.name", "Engine fixture")
    git(checkout, "config", "user.email", "engine@example.test")
    hooks = base / "hooks"
    hooks.mkdir()
    git(checkout, "config", "core.hooksPath", str(hooks))
    (checkout / "README.md").write_text("Fixture Cyber Bench repository\n")
    git(checkout, "add", "--", "README.md")
    git(checkout, "commit", "--no-gpg-sign", "-m", "[Infra] Initial fixture")
    git(checkout, "remote", "add", "origin", str(remote))
    git(checkout, "push", "origin", "HEAD:refs/heads/main")
    return checkout, remote


class EngineFixture(unittest.TestCase):
    def setUp(self: Self) -> None:
        # Each test owns a temporary project, outside the source tree, removed on cleanup.
        temporary = tempfile.TemporaryDirectory(prefix="v2-engine-case-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "tasks"
        self.root.mkdir()
        self.config = self.base / "actions.json"
        self.set_actions(
            {
                "prepare": {"dependencies": []},
                "process": {"dependencies": ["prepare"]},
                "summarize": {"dependencies": ["process"]},
            }
        )
        self.task_directory = self.root / "set-one" / "task-one"
        self.review = self.task_directory / ".review"
        self.set_review = self.task_directory.parent / ".review"
        self.task_directory.mkdir(parents=True)
        (self.task_directory.parent / "template.md").write_text("# Template\n", "utf-8")
        (self.task_directory / "design.md").write_text("# Design\n", "utf-8")

    def make_engine(
        self: Self,
        *,
        workers: bool = False,
        initiate: Callable[[str, str, ActionState], None] | None = None,
        **options: Unpack[EngineOptions],
    ) -> Engine:
        def controlled_runner(engine: Engine) -> ActionRunner:
            # State tests control launches and reconcile real result files explicitly.
            runner = ActionRunner(engine)
            for name, replacement in (
                ("initiate", initiate if initiate is not None else mock.Mock()),
                ("recover", mock.Mock()),
                ("start_monitor", mock.Mock()),
            ):
                patcher = mock.patch.object(runner, name, replacement)
                patcher.start()
                self.addCleanup(patcher.stop)
            return runner

        if workers:
            engine = Engine(self.root, actions_file=self.config, **options)
        else:
            with mock.patch(
                "engine.runner.ActionRunner", side_effect=controlled_runner
            ):
                engine = Engine(self.root, actions_file=self.config, **options)
        self.addCleanup(engine.close)
        return engine

    def set_actions(self: Self, definitions: Mapping[str, JsonObject]) -> None:
        self.historical_definitions = copy.deepcopy(dict(definitions))
        definitions = copy.deepcopy(dict(definitions))
        for definition in definitions.values():
            definition.pop("dependencies", None)
            definition.setdefault("handler", "action_command")
            if definition["handler"] == "action_command":
                definition.setdefault(
                    "runner", {"argv": [sys.executable, "-c", "pass"]}
                )
        self.config.write_text(
            json.dumps({"schema_version": 1, "actions": definitions}), "utf-8"
        )

    def command(
        self: Self,
        engine: Engine,
        name: str,
        *,
        request_id: str | None = None,
        parameters: JsonObject | None = None,
    ) -> ActionReceipt:
        body: RequestBody = {
            "request_id": request_id or str(uuid.uuid4()),
            "action": name,
        }
        if parameters is not None:
            body["parameters"] = parameters
        return action_receipt(engine.command("set-one", "task-one", body))

    def legacy_command(
        self: Self,
        engine: Engine,
        name: str,
        *,
        request_id: str | None = None,
        parameters: JsonObject | None = None,
        mode: str | None = None,
    ) -> ActionReceipt:
        """Create a pre-refactor waiting event to exercise real history compatibility."""
        definition = engine.actions[name]
        request: RequestBody = {
            "request_id": request_id or str(uuid.uuid4()),
            "action": name,
            "parameters": parameters or {},
        }
        payload: EventPayload = {
            "action": name,
            "parameters": request["parameters"],
            "dependencies": cast(
                list[str], self.historical_definitions[name].get("dependencies", [])
            ),
            "execution": historical_execution(definition, request["parameters"]),
        }
        if "execution_type" in definition:
            payload["execution_type"] = definition["execution_type"]
        if mode is not None:
            payload["mode"] = mode
        return action_receipt(
            engine._commit(
                ("set-one", "task-one"),
                request,
                "command",
                "waiting",
                str(uuid.uuid4()),
                payload,
            )
        )

    def finish(
        self: Self,
        engine: Engine,
        action_id: str,
        *,
        outcome: Outcome = "succeeded",
        result: JsonValue = None,
        error: str | None = None,
        failure: FailureInfo | None = None,
    ) -> ActionReceipt:
        key = ("set-one", "task-one")
        directory = job_directory(self.root, key, action_id, create=True)
        atomic_json_write(
            directory / "result.json",
            {"outcome": outcome, "result": result, "error": error, "failure": failure},
        )
        with engine._lock:
            action = engine._states[key]["actions"][action_id]
            completion = engine._runner._completion(key, action)
            assert completion is not None
            engine._runner._apply(key, action_id, completion)
            return action_receipt(
                acknowledgment(
                    key, engine._requests[key][f"engine:completion:{action_id}"]
                )
            )

    def complete_action(self: Self, engine: Engine, name: str) -> str:
        acknowledgment = self.command(engine, name)
        self.finish(engine, acknowledgment["action_id"])
        return acknowledgment["action_id"]


def historical_execution(
    definition: ActionDefinition, parameters: JsonObject
) -> ExecutionSpec:
    """Prepare an old invocation while retaining its original parameter contents."""
    from engine.actions.registry import handler_for

    values = copy.deepcopy(parameters)
    for name in handler_for(definition["handler"]).required_parameters:
        if name not in values:
            values[name] = "historical-source" if name == "source_id" else {}
    return prepare_execution(definition, values)


class HistoricalEngineFixture(EngineFixture):
    """Generate schema v1 events to exercise supported historical recovery paths.

    These fixtures do not represent the current REST submission interface.
    New command acceptance and scheduling are covered in test_commands/test_server.
    """

    def command(
        self: Self,
        engine: Engine,
        name: str,
        *,
        request_id: str | None = None,
        parameters: JsonObject | None = None,
    ) -> ActionReceipt:
        body: RequestBody = {
            "request_id": request_id or str(uuid.uuid4()),
            "action": name,
            "parameters": parameters or {},
        }
        validate_body(body, ("request_id", "action", "parameters"))
        validate_name(name)
        with engine._lock:
            engine._check_available()
            key = engine._key("set-one", "task-one")
            previous = engine._duplicate(
                key, body["request_id"], fingerprint("command", body)
            )
            if previous is not None:
                return action_receipt(previous)
            engine._ensure_discovered(key)
            definition = engine.actions[name]
            action_id = str(uuid.uuid4())
            payload: EventPayload = {
                "action": name,
                "parameters": body["parameters"],
                "dependencies": cast(
                    list[str], self.historical_definitions[name].get("dependencies", [])
                ),
                "execution": historical_execution(definition, body["parameters"]),
            }
            if "execution_type" in definition:
                payload["execution_type"] = definition["execution_type"]
            try:
                references = dependency_references(engine._states[key], payload)
            except EngineError as error:
                if error.code != "dependencies_not_ready":
                    raise
                receipt = engine._commit(
                    key, body, "command", "waiting", action_id, payload
                )
            else:
                payload["dependency_actions"] = references
                receipt = engine._commit(
                    key, body, "command", "started", action_id, payload
                )
                engine._initiate(key, action_id)
            engine._start_ready(key)
            return action_receipt(receipt)


class WorkerFixture(EngineFixture):
    def clean_workers(self: Self) -> None:
        # Own only our temporary fixture's workers; cleanup runs even on assertion failures.
        jobs = list(self.root.glob("*/*/.review/jobs/engine/*"))
        for directory in jobs:
            (directory / "cancel.json").write_text("{}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            living = []
            for directory in jobs:
                for name in ("worker.json", "launch.json"):
                    try:
                        record = json.loads((directory / name).read_text())
                        if same_process(record.get("pid"), record.get("identity")):
                            living.append(record["pid"])
                    except (OSError, ValueError):
                        pass
            if not living:
                break
            time.sleep(0.05)
        for directory in jobs:
            try:
                process = json.loads((directory / "process.json").read_text())
                if same_process(process.get("pid"), process.get("identity")):
                    stop_group(process["pid"], 0.1)
            except (OSError, ValueError):
                pass
        for pid in living if jobs else []:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def script(
        self: Self,
        text: str,
        *,
        dependencies: Sequence[str] = (),
        **runner: Unpack[ExecutionSpec],
    ) -> JsonObject:
        return {
            "dependencies": list(dependencies),
            "execution_type": "script",
            "handler": "action_command",
            "runner": cast(
                JsonObject,
                {
                    "argv": [sys.executable, "-c", text],
                    "termination_grace_ms": 100,
                    **runner,
                },
            ),
        }

    def wait_for(
        self: Self, condition: Callable[[], T | None], timeout: float = 8
    ) -> T:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.05)
        self.fail("Timed out waiting for execution")

    def finished(self: Self, engine: Engine, action_id: str) -> ActionState:
        return self.wait_for(
            lambda: (
                lambda action: action if action["status"] == "completed" else None
            )(engine.state("set-one", "task-one")["actions"][action_id])
        )
