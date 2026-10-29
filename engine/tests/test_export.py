from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from typing import Callable, Iterable, Self
from unittest.mock import patch

from engine.actions import load_actions
from engine.git_worker.export import export_task
from engine.json_data import text_value
from engine.tests.support import EngineFixture, export_repository, git
from engine.type_defs import ExecutionCommand, JsonObject, PathLike


class ExportTests(EngineFixture):
    def setUp(self: Self) -> None:
        super().setUp()
        self.checkout, self.remote = export_repository(self.base)
        self.name = "cyber-bench-fixture-1.1-task"
        self.destination = self.checkout / self.name
        (self.task_directory / "task.toml").write_text(
            f'[task]\nname = "{self.name}"\n'
        )
        environment = self.task_directory / "environment"
        environment.mkdir()
        self.script = environment / "run.sh"
        self.script.write_text("#!/bin/sh\nexit 0\n")
        self.script.chmod(0o755)
        self.commands: list[list[str]] = []

    def execute(self: Self, command: ExecutionCommand) -> int:
        self.commands.append(command["argv"])
        return subprocess.run(
            command["argv"],
            cwd=command["cwd"],
            env={**os.environ, **command.get("env", {})},
            capture_output=True,
            text=True,
            timeout=15,
        ).returncode

    def export(self: Self) -> JsonObject:
        return export_task(self.task_directory, self.checkout, self.execute)

    def test_copy_commit_push_uses_declared_name_and_omits_local_files(
        self: Self,
    ) -> None:
        self.review.mkdir()
        (self.review / "events.jsonl").write_text("Local state")
        remote_artifacts = self.task_directory / ".remote"
        remote_artifacts.mkdir()
        (remote_artifacts / "cache.json").write_text("Local cache")
        (self.task_directory / "design-status.json").write_text("Local status")
        (self.task_directory / "alias").symlink_to(self.script)
        (self.task_directory / "instruction.md").write_text("Fixture instruction")
        (self.task_directory / "évidence.txt").write_text("Fixture evidence")
        result = self.export()
        self.assertEqual(
            result["commit"], git(self.remote, "rev-parse", "refs/heads/main")
        )
        self.assertEqual(result["branch"], "main")
        self.assertTrue(result["pushed"])
        self.assertEqual(
            (self.destination / "environment/run.sh").read_bytes(),
            self.script.read_bytes(),
        )
        self.assertEqual(
            (self.destination / "environment/run.sh").stat().st_mode & 0o111, 0o111
        )
        for omitted in (
            "design.md",
            "design-status.json",
            ".review",
            ".remote",
            "alias",
        ):
            self.assertFalse((self.destination / omitted).exists())
        self.assertTrue(
            git(self.checkout, "log", "-1", "--format=%s").startswith("[FIXTURE 1.1]")
        )
        self.assertEqual(git(self.checkout, "status", "--porcelain"), "")

    def test_updates_remove_old_files_and_preserve_unrelated_staging(
        self: Self,
    ) -> None:
        old = self.task_directory / "old.txt"
        old.write_text("Old payload")
        self.export()
        (self.checkout / "README.md").write_text("Unrelated staged work")
        git(self.checkout, "add", "--", "README.md")
        before = git(self.checkout, "diff", "--cached", "--", "README.md")
        (self.checkout / "notes.txt").write_text("Unrelated untracked work")
        old.unlink()
        self.script.write_text("#!/bin/sh\nexit 1\n")
        result = self.export()
        self.assertFalse((self.destination / "old.txt").exists())
        self.assertEqual(
            git(self.checkout, "diff", "--cached", "--", "README.md"), before
        )
        paths = git(
            self.checkout,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            text_value(result["commit"]),
        ).splitlines()
        self.assertTrue(all(path.startswith(self.name + "/") for path in paths))
        self.assertEqual(
            (self.checkout / "notes.txt").read_text(), "Unrelated untracked work"
        )

    def test_dirty_destination_is_preserved(self: Self) -> None:
        self.export()
        file = self.destination / "task.toml"
        file.write_text("Uncommitted destination work")
        remote = git(self.remote, "rev-parse", "refs/heads/main")
        with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
            self.export()
        self.assertEqual(file.read_text(), "Uncommitted destination work")
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), remote)

    def test_commit_failure_restores_destination_and_task_index(self: Self) -> None:
        self.export()
        original = (self.destination / "environment/run.sh").read_bytes()
        self.script.write_text("Changed source")
        hook = self.base / "hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "failed with exit status"):
            self.export()
        self.assertEqual(
            (self.destination / "environment/run.sh").read_bytes(), original
        )
        self.assertEqual(
            git(self.checkout, "status", "--porcelain", "--", self.name), ""
        )

    def test_push_failure_keeps_commit_for_an_explicit_retry(self: Self) -> None:
        initial = git(self.remote, "rev-parse", "refs/heads/main")
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "was saved locally"):
            self.export()
        saved = git(self.checkout, "rev-parse", "HEAD")
        self.assertNotEqual(saved, initial)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), initial)
        hook.unlink()
        self.assertEqual(self.export()["push_commit"], saved)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), saved)

    def test_push_helper_receives_exact_task_and_can_acquire_commit_lock(
        self: Self,
    ) -> None:
        helper = self.checkout / ".claude/scripts/home-git-push"
        helper.parent.mkdir(parents=True)
        helper.write_text(
            "#!/bin/sh\n"
            'test "$1" = task || exit 2\n'
            f'test "$2" = {self.name} || exit 2\n'
            "flock -n .git/cyber-bench-commit.lock git push origin HEAD:refs/heads/main\n"
        )
        helper.chmod(0o755)
        self.assertTrue(self.export()["pushed"])
        self.assertIn([str(helper), "task", self.name], self.commands)
        self.assertFalse(
            any(arguments[:2] == ["git", "push"] for arguments in self.commands)
        )

    def test_unchanged_export_does_not_push_again(self: Self) -> None:
        first = self.export()
        self.commands.clear()
        second = self.export()
        self.assertEqual(second["push_commit"], first["push_commit"])
        self.assertTrue(second["already_exported"])
        self.assertFalse(
            any(arguments[:2] == ["git", "push"] for arguments in self.commands)
        )

    def test_export_lock_contention_does_not_copy(self: Self) -> None:
        with (self.checkout / ".git/codimango-export.lock").open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "Another Git operation"):
                self.export()
        self.assertFalse(self.destination.exists())

    def test_unpublished_unrelated_commits_are_not_pushed_without_helper(
        self: Self,
    ) -> None:
        (self.checkout / "README.md").write_text("Deferred infrastructure work")
        git(self.checkout, "add", "--", "README.md")
        git(self.checkout, "commit", "--no-gpg-sign", "-m", "[Infra] Deferred work")
        initial = git(self.remote, "rev-parse", "refs/heads/main")
        with self.assertRaisesRegex(RuntimeError, "outside this task"):
            self.export()
        self.assertFalse(self.destination.exists())
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), initial)

    def test_source_changes_during_copy_stop_before_commit(self: Self) -> None:

        original = shutil.copytree

        def copy(
            src: PathLike,
            dst: PathLike,
            symlinks: bool = False,
            ignore: Callable[[PathLike, list[str]], Iterable[str]] | None = None,
            copy_function: Callable[[str, str], object] = shutil.copy2,
            ignore_dangling_symlinks: bool = False,
            dirs_exist_ok: bool = False,
        ) -> PathLike:
            result = original(
                src,
                dst,
                symlinks,
                ignore,
                copy_function,
                ignore_dangling_symlinks,
                dirs_exist_ok,
            )
            self.script.write_text("Concurrent source change")
            return result

        with patch("engine.git_worker.export.shutil.copytree", side_effect=copy):
            with self.assertRaisesRegex(RuntimeError, "changed while copying"):
                self.export()
        self.assertFalse(self.destination.exists())

    def test_repository_path_is_resolved_relative_to_actions_file(self: Self) -> None:
        self.set_actions(
            {
                "export": {
                    "dependencies": [],
                    "handler": "action_export_to_codimango",
                    "runner": {"repo_path": "cyber-bench"},
                }
            }
        )
        self.assertEqual(
            load_actions(self.config)["export"]["runner"]["repo_path"],
            str(self.checkout),
        )
