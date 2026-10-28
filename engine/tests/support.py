import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from engine.core import ENGINE_DIRECTORY, Engine


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
