import json
import tempfile
import unittest
import uuid
from pathlib import Path

from engine.core import ENGINE_DIRECTORY, Engine


class EngineFixture(unittest.TestCase):
    def setUp(self):
        # Each test owns a temporary project, outside the source tree, removed on cleanup.
        temporary = tempfile.TemporaryDirectory(prefix="v2-engine-case-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "tasks"
        self.root.mkdir()
        self.config = self.base / "actions.json"
        self.config.write_text((ENGINE_DIRECTORY / "actions.json").read_text("utf-8"), "utf-8")
        self.task_directory = self.root / "set-one" / "task-one"
        self.review = self.task_directory / ".review"
        self.set_review = self.task_directory.parent / ".review"
        self.task_directory.mkdir(parents=True)
        (self.task_directory.parent / "template.md").write_text("# Template\n", "utf-8")
        (self.task_directory / "design.md").write_text("# Design\n", "utf-8")

    def make_engine(self, **options):
        engine = Engine(self.root, actions_file=self.config, **options)
        self.addCleanup(engine.close)
        return engine

    def set_actions(self, definitions):
        self.config.write_text(json.dumps({"schema_version": 1, "actions": definitions}), "utf-8")

    def command(self, engine, name, *, request_id=None, parameters=None):
        body = {"request_id": request_id or str(uuid.uuid4()), "action": name}
        if parameters is not None:
            body["parameters"] = parameters
        return engine.command("set-one", "task-one", body)

    def report(self, engine, action_id, kind, *, outcome="succeeded", request_id=None, **details):
        body = {
            "request_id": request_id or str(uuid.uuid4()),
            "action_id": action_id,
            "type": kind,
        }
        if kind == "completed":
            body.update({"outcome": outcome, **details})
        return engine.complete("set-one", "task-one", body)

    def complete_action(self, engine, name):
        acknowledgment = self.command(engine, name)
        self.report(engine, acknowledgment["action_id"], "completed")
        return acknowledgment["action_id"]
