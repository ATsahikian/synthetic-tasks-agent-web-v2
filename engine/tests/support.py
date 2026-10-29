import copy
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from engine.actions import prepare_execution, fingerprint, validate_name
from engine.core import validate_body
from engine.commands.legacy import dependency_references
from engine.errors import EngineError
from engine.core import Engine, acknowledgment
from engine.runner import ActionRunner, job_directory
from engine.storage import atomic_json_write


def git(directory, *arguments):
    return subprocess.run(
        ["git", "-C", str(directory), *arguments], check=True,
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()


def task_repository(root):
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Engine fixture")
    git(root, "config", "user.email", "engine@example.test")
    git(root, "config", "core.hooksPath", str(root / ".git" / "hooks"))
    (root / ".gitignore").write_text("**/.review/\n")
    git(root, "add", "--", ".gitignore")
    git(root, "commit", "--no-gpg-sign", "-m", "Initial task project")


def export_repository(base):
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
    def setUp(self):
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

    def make_engine(self, *, workers=False, initiate=None, **options):
        def controlled_runner(engine):
            # State tests control launches and reconcile real result files explicitly.
            runner = ActionRunner(engine)
            runner.initiate = initiate if initiate is not None else mock.Mock()
            runner.recover = mock.Mock()
            runner.start_monitor = mock.Mock()
            return runner

        if workers:
            engine = Engine(self.root, actions_file=self.config, **options)
        else:
            with mock.patch("engine.runner.ActionRunner", side_effect=controlled_runner):
                engine = Engine(self.root, actions_file=self.config, **options)
        self.addCleanup(engine.close)
        return engine

    def set_actions(self, definitions):
        self.historical_definitions = copy.deepcopy(definitions)
        definitions = copy.deepcopy(definitions)
        for definition in definitions.values():
            definition.pop("dependencies", None)
            definition.setdefault("handler", "action_command")
            if definition["handler"] == "action_command":
                definition.setdefault("runner", {"argv": [sys.executable, "-c", "pass"]})
        self.config.write_text(json.dumps({"schema_version": 1, "actions": definitions}), "utf-8")

    def command(self, engine, name, *, request_id=None, parameters=None):
        body = {"request_id": request_id or str(uuid.uuid4()), "action": name}
        if parameters is not None:
            body["parameters"] = parameters
        return engine.command("set-one", "task-one", body)

    def legacy_command(self, engine, name, *, request_id=None, parameters=None, mode=None):
        """Create a pre-refactor waiting event to exercise real history compatibility."""
        definition = engine.actions[name]
        request = {"request_id": request_id or str(uuid.uuid4()), "action": name, "parameters": parameters or {}}
        payload = {
            "action": name,
            "parameters": request["parameters"],
            "dependencies": self.historical_definitions[name].get("dependencies", []),
            "execution": historical_execution(definition, request["parameters"]),
        }
        if "execution_type" in definition:
            payload["execution_type"] = definition["execution_type"]
        if mode is not None:
            payload["mode"] = mode
        return engine._commit(
            ("set-one", "task-one"), request, "command", "waiting", str(uuid.uuid4()), payload,
        )

    def finish(self, engine, action_id, *, outcome="succeeded", result=None, error=None):
        key = ("set-one", "task-one")
        directory = job_directory(self.root, key, action_id, create=True)
        atomic_json_write(
            directory / "result.json", {"outcome": outcome, "result": result, "error": error}
        )
        with engine._lock:
            action = engine._states[key]["actions"][action_id]
            completion = engine._runner._completion(key, action)
            engine._runner._apply(key, action_id, completion)
            return acknowledgment(key, engine._requests[key][f"engine:completion:{action_id}"])

    def complete_action(self, engine, name):
        acknowledgment = self.command(engine, name)
        self.finish(engine, acknowledgment["action_id"])
        return acknowledgment["action_id"]


def historical_execution(definition, parameters):
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

    def command(self, engine, name, *, request_id=None, parameters=None):
        body = {"request_id": request_id or str(uuid.uuid4()), "action": name, "parameters": parameters or {}}
        validate_body(body, ("request_id", "action", "parameters"))
        validate_name(name)
        with engine._lock:
            engine._check_available()
            key = engine._key("set-one", "task-one")
            previous = engine._duplicate(key, body["request_id"], fingerprint("command", body))
            if previous is not None:
                return previous
            engine._ensure_discovered(key)
            definition = engine.actions[name]
            action_id = str(uuid.uuid4())
            payload = {
                "action": name, "parameters": body["parameters"],
                "dependencies": self.historical_definitions[name].get("dependencies", []),
                "execution": historical_execution(definition, body["parameters"]),
                "mode": definition["mode"],
            }
            if "execution_type" in definition:
                payload["execution_type"] = definition["execution_type"]
            try:
                references = dependency_references(engine._states[key], payload)
            except EngineError as error:
                if error.code != "dependencies_not_ready":
                    raise
                receipt = engine._commit(key, body, "command", "waiting", action_id, payload)
            else:
                payload["dependency_actions"] = references
                if definition["mode"] == "sync":
                    action = {"action_id": action_id, "name": name, **payload}
                    payload.update(engine._execute_sync(key, action))
                    receipt = engine._commit(key, body, "command", "completed", action_id, payload)
                else:
                    receipt = engine._commit(key, body, "command", "started", action_id, payload)
                    engine._initiate(key, action_id)
            engine._start_ready(key)
            return receipt
