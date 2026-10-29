from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Self

from engine.actions.common import design_sources
from engine.json_data import object_value, text_value
from engine.runner import job_directory
from engine.tests.support import ActionReceipt, WorkerFixture, git, task_repository
from engine.type_defs import ActionState, JsonValue

if TYPE_CHECKING:
    from engine.core import Engine


class CommitDesingTests(WorkerFixture):
    def setUp(self: Self) -> None:
        super().setUp()
        self.set_actions({"commit_desing": {"handler": "action_commit_desing"}})

    def commit(
        self: Self,
        engine: Engine,
        *,
        request_id: str | None = None,
        source: Mapping[str, JsonValue] | None = None,
    ) -> tuple[ActionReceipt, ActionState]:
        receipt = self.command(
            engine,
            "commit_desing",
            request_id=request_id,
            parameters={"source": dict(source or design_sources(self.task_directory))},
        )
        action = self.finished(engine, receipt["action_id"])
        return receipt, action

    def test_commit_contains_only_design_and_preserves_unrelated_staged_files(
        self: Self,
    ) -> None:
        task_repository(self.root)
        (self.root / "notes.txt").write_text("Other work\n")
        git(self.root, "add", "--", "notes.txt")
        engine = self.make_engine(workers=True)
        receipt, action = self.commit(engine, request_id="commit-once")
        self.assertEqual(
            (receipt["type"], action["outcome"]), ("command_started", "succeeded")
        )
        result = object_value(action["result"])
        self.assertTrue(result["approved"] and result["committed"])
        self.assertEqual(
            result["design_sha256"],
            design_sources(self.task_directory)["design_sha256"],
        )
        self.assertEqual(result["commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(
            git(
                self.root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                text_value(result["commit"]),
            ),
            "set-one/task-one/design.md",
        )
        self.assertEqual(
            git(
                self.root,
                "show",
                text_value(result["commit"]) + ":set-one/task-one/design.md",
            ),
            "# Design",
        )
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "notes.txt")
        self.assertIsNotNone(action["started_at"])
        directory = job_directory(
            self.root, ("set-one", "task-one"), receipt["action_id"]
        )
        self.assertTrue((directory / "git-transaction.json").exists())
        self.assertEqual(
            self.command(
                engine,
                "commit_desing",
                request_id="commit-once",
                parameters={"source": {**design_sources(self.task_directory)}},
            ),
            receipt,
        )
        self.assertEqual(git(self.root, "rev-list", "--count", "HEAD"), "2")

    def test_unchanged_design_reuses_commit_and_changed_design_creates_another(
        self: Self,
    ) -> None:
        task_repository(self.root)
        engine = self.make_engine(workers=True)
        first = object_value(self.commit(engine)[1]["result"])
        repeat = object_value(self.commit(engine)[1]["result"])
        self.assertEqual(repeat["commit"], first["commit"])
        self.assertFalse(repeat["committed"])
        (self.task_directory / "design.md").write_text("# Revised design\n")
        changed = object_value(self.commit(engine)[1]["result"])
        self.assertTrue(changed["committed"])
        self.assertNotEqual(changed["commit"], first["commit"])
        self.assertEqual(git(self.root, "rev-list", "--count", "HEAD"), "3")

    def test_design_commit_rejects_changed_inputs(self: Self) -> None:
        task_repository(self.root)
        engine = self.make_engine(workers=True)
        head = git(self.root, "rev-parse", "HEAD")
        for path in (
            self.task_directory / "design.md",
            self.task_directory.parent / "template.md",
        ):
            with self.subTest(path=path):
                source = design_sources(self.task_directory)
                original = path.read_text()
                path.write_text(original + "Changed after redesign\n")
                action = self.commit(engine, source=source)[1]
                self.assertEqual(action["outcome"], "failed")
                self.assertIn("Design or template changed", text_value(action["error"]))
                self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
                self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
                path.write_text(original)

    def test_task_outside_git_fails_with_clear_error(self: Self) -> None:
        engine = self.make_engine(workers=True)
        action = self.commit(engine)[1]
        self.assertEqual(action["outcome"], "failed")
        self.assertIn("inside a Git repository", text_value(action["error"]))

    def test_commit_failure_blocks_approval_and_can_be_retried(self: Self) -> None:
        task_repository(self.root)
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'fixture commit rejected' >&2\nexit 1\n")
        hook.chmod(0o700)
        engine = self.make_engine(workers=True)
        head = git(self.root, "rev-parse", "HEAD")
        action = self.commit(engine)[1]
        self.assertEqual(action["outcome"], "failed")
        self.assertIsNone(action["result"])
        self.assertIn("fixture commit rejected", text_value(action["error"]))
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        hook.unlink()
        self.assertEqual(self.commit(engine)[1]["outcome"], "succeeded")
