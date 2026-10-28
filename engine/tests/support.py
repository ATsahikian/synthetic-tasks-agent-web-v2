import copy
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from engine.core import Engine, acknowledgment
from engine.runner import ActionRunner, job_directory
from engine.storage import atomic_json_write


def git(directory, *arguments):
    return subprocess.run(
        ["git", "-C", str(directory), *arguments], check=True,
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()


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
        definitions = copy.deepcopy(definitions)
        for definition in definitions.values():
            definition.setdefault("runner", {"argv": [sys.executable, "-c", "pass"]})
        self.config.write_text(json.dumps({"schema_version": 1, "actions": definitions}), "utf-8")

    def command(self, engine, name, *, request_id=None, parameters=None):
        body = {"request_id": request_id or str(uuid.uuid4()), "action": name}
        if parameters is not None:
            body["parameters"] = parameters
        return engine.command("set-one", "task-one", body)

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
