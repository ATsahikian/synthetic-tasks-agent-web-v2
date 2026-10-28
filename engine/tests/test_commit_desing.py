import json

from engine.actions.common import design_sources
from engine.core import ENGINE_DIRECTORY
from engine.tests.support import EngineFixture, git, task_repository


class CommitDesingTests(EngineFixture):
    def setUp(self):
        super().setUp()
        definition = json.loads((ENGINE_DIRECTORY / "actions" / "actions.json").read_text())["actions"]["commit_desing"]
        self.set_actions({"redesign": {"dependencies": []}, "commit_desing": definition})

    def redesign(self, engine):
        action = self.command(engine, "redesign")["action_id"]
        self.finish(engine, action, result=design_sources(self.task_directory))
        return action

    def test_commit_contains_only_design_and_preserves_unrelated_staged_files(self):
        task_repository(self.root)
        unrelated = self.root / "notes.txt"
        unrelated.write_text("Other work\n")
        git(self.root, "add", "--", "notes.txt")
        engine = self.make_engine()
        redesign = self.redesign(engine)
        receipt = self.command(engine, "commit_desing", request_id="commit-once")
        self.assertEqual((receipt["type"], receipt["outcome"]), ("completed", "succeeded"))
        result = receipt["result"]
        self.assertTrue(result["approved"])
        self.assertTrue(result["committed"])
        self.assertEqual(result["design_sha256"], design_sources(self.task_directory)["design_sha256"])
        self.assertEqual(result["commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(
            git(self.root, "diff-tree", "--no-commit-id", "--name-only", "-r", result["commit"]),
            "set-one/task-one/design.md",
        )
        self.assertEqual(git(self.root, "show", f"{result['commit']}:set-one/task-one/design.md"), "# Design")
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "notes.txt")
        state = engine.state("set-one", "task-one")["actions"][receipt["action_id"]]
        self.assertEqual(state["dependency_actions"], {"redesign": redesign})
        self.assertIsNone(state["started_at"])
        self.assertFalse((self.review / "jobs" / receipt["action_id"]).exists())
        self.assertEqual(self.command(engine, "commit_desing", request_id="commit-once"), receipt)
        self.assertEqual(git(self.root, "rev-list", "--count", "HEAD"), "2")

    def test_unchanged_design_reuses_commit_and_changed_design_creates_another(self):
        task_repository(self.root)
        engine = self.make_engine()
        self.redesign(engine)
        first = self.command(engine, "commit_desing")["result"]
        repeat = self.command(engine, "commit_desing")["result"]
        self.assertEqual(repeat["commit"], first["commit"])
        self.assertFalse(repeat["committed"])
        (self.task_directory / "design.md").write_text("# Revised design\n")
        self.redesign(engine)
        changed = self.command(engine, "commit_desing")["result"]
        self.assertTrue(changed["committed"])
        self.assertNotEqual(changed["commit"], first["commit"])
        self.assertEqual(git(self.root, "rev-list", "--count", "HEAD"), "3")

    def test_design_commit_rejects_changed_inputs(self):
        task_repository(self.root)
        engine = self.make_engine()
        head = git(self.root, "rev-parse", "HEAD")
        for path in (self.task_directory / "design.md", self.task_directory.parent / "template.md"):
            with self.subTest(path=path):
                self.redesign(engine)
                original = path.read_text()
                path.write_text(original + "Changed after redesign\n")
                receipt = self.command(engine, "commit_desing")
                self.assertEqual(receipt["outcome"], "failed")
                self.assertIn("Design or template changed", receipt["error"])
                self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
                self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
                path.write_text(original)

    def test_task_outside_git_fails_with_clear_error(self):
        engine = self.make_engine()
        self.redesign(engine)
        receipt = self.command(engine, "commit_desing")
        self.assertEqual(receipt["outcome"], "failed")
        self.assertIn("inside a Git repository", receipt["error"])

    def test_commit_failure_blocks_approval_and_can_be_retried(self):
        task_repository(self.root)
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'fixture commit rejected' >&2\nexit 1\n")
        hook.chmod(0o700)
        engine = self.make_engine()
        self.redesign(engine)
        head = git(self.root, "rev-parse", "HEAD")
        receipt = self.command(engine, "commit_desing")
        self.assertEqual(receipt["outcome"], "failed")
        self.assertIsNone(receipt["result"])
        self.assertIn("fixture commit rejected", receipt["error"])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        hook.unlink()
        self.assertEqual(self.command(engine, "commit_desing")["outcome"], "succeeded")
