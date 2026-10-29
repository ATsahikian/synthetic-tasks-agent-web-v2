"""Real repositories and abrupt process exits exercise the Git queue contract."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Self
from unittest.mock import patch

from engine.actions.action_commit_desing import action_commit_desing
from engine.actions.action_export_to_codimango import action_export_to_codimango
from engine.actions.common import design_sources
from engine.core import Engine
from engine.errors import CorruptLogError, EngineError
from engine.json_data import object_value, text_value
from engine.tests.support import (
    ActionReceipt,
    WorkerFixture,
    action_receipt,
    export_repository,
    git,
    task_repository,
)
from engine.type_defs import (
    ActionConfig,
    ActionState,
    ExecutionCommand,
    JsonObject,
)


class GitWorkerTests(WorkerFixture):
    def setUp(self: Self) -> None:
        super().setUp()
        task_repository(self.root)
        self.set_actions(
            {
                "design": {"handler": "action_commit_desing"},
                "implementation": {"handler": "action_commit_impl"},
                "other": {"handler": "action_command"},
            }
        )

    def submit(
        self: Self,
        engine: Engine,
        task: Path | None = None,
        action: str = "design",
        request_id: str = "commit",
    ) -> ActionReceipt:
        task = task or self.task_directory
        source = {**design_sources(task), "oracle_passed": True}
        return action_receipt(
            engine.command(
                task.parent.name,
                task.name,
                {
                    "request_id": request_id,
                    "action": action,
                    "parameters": {"source": source},
                },
            )
        )

    def done(self: Self, engine: Engine, task: Path, action_id: str) -> ActionState:
        self.wait_for(
            lambda: (
                engine.state(task.parent.name, task.name)["actions"][action_id][
                    "status"
                ]
                == "completed"
            )
        )
        return engine.state(task.parent.name, task.name)["actions"][action_id]

    def sibling(self: Self, task_set: str = "set-one", name: str = "task-two") -> Path:
        task = self.root / task_set / name
        task.mkdir(parents=True)
        (task.parent / "template.md").write_text("# Template\n")
        (task / "design.md").write_text(f"# {name}\n")
        return task

    def test_one_thread_serializes_tasks_and_keeps_api_and_other_actions_responsive(
        self: Self,
    ) -> None:
        second = self.sibling("set-two")
        third = self.sibling("set-two", "task-three")
        (self.root / "notes.txt").write_text("Unrelated staging")
        git(self.root, "add", "notes.txt")
        entered, release = threading.Event(), threading.Event()
        calls = []
        original = action_commit_desing.run

        def blocked(
            handler: action_commit_desing,
            config: ActionConfig,
            execute: Callable[[ExecutionCommand], int],
            output: Path,
        ) -> JsonObject:
            calls.append((threading.current_thread().name, config["task_path"]))
            if len(calls) == 1:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("Test did not release Git worker")
            return original(handler, config, execute, output)

        with patch.object(action_commit_desing, "run", blocked):
            engine = self.make_engine(workers=True)
            try:
                first = self.submit(engine)
                self.assertTrue(entered.wait(3))
                other = self.submit(engine, second)
                self.assertEqual(engine.health()["status"], "ok")
                self.assertEqual(len(calls), 1)
                state = engine.state("set-two", "task-two")["actions"][
                    text_value(other["action_id"])
                ]
                self.assertEqual(state["status"], "started")
                self.assertEqual(self.submit(engine), first)
                detached = engine.command(
                    "set-two", "task-three", {"request_id": "other", "action": "other"}
                )
                self.assertEqual(
                    self.done(engine, third, text_value(detached["action_id"]))[
                        "outcome"
                    ],
                    "succeeded",
                )
                self.assertEqual(len(calls), 1)
                release.set()
                self.assertEqual(
                    self.done(
                        engine, self.task_directory, text_value(first["action_id"])
                    )["outcome"],
                    "succeeded",
                )
                self.assertEqual(
                    self.done(engine, second, text_value(other["action_id"]))[
                        "outcome"
                    ],
                    "succeeded",
                )
            finally:
                release.set()
                engine.close()
        self.assertEqual([name for name, _ in calls], ["git-worker", "git-worker"])
        commits = git(self.root, "log", "-2", "--format=%H").splitlines()
        self.assertEqual(
            git(
                self.root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                commits[0],
            ),
            "set-two/task-two/design.md",
        )
        self.assertEqual(
            git(
                self.root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                commits[1],
            ),
            "set-one/task-one/design.md",
        )
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "notes.txt")

    def test_implementation_commits_additions_deletions_and_modes_without_runtime_files(
        self: Self,
    ) -> None:
        old = self.task_directory / "old.txt"
        old.write_text("remove me")
        git(self.root, "add", "set-one/task-one/old.txt")
        git(self.root, "commit", "--no-gpg-sign", "-m", "Previous implementation")
        old.unlink()
        script = self.task_directory / "run.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        (self.task_directory / ".remote").mkdir()
        (self.task_directory / ".remote" / "cache").write_text("omit")
        engine = self.make_engine(workers=True)
        receipt = self.submit(engine, action="implementation")
        action = self.finished(engine, text_value(receipt["action_id"]))
        self.assertEqual(action["outcome"], "succeeded", action["error"])
        result = object_value(action["result"])
        self.assertTrue(result["approved"] and result["committed"])
        files = git(
            self.root, "ls-tree", "-r", "--name-only", text_value(result["commit"])
        )
        self.assertIn("set-one/task-one/run.sh", files)
        self.assertNotIn("old.txt", files)
        self.assertNotIn(".review", files)
        self.assertNotIn(".remote", files)
        self.assertTrue(
            git(
                self.root,
                "ls-tree",
                text_value(result["commit"]),
                "set-one/task-one/run.sh",
            ).startswith("100755")
        )
        repeated = self.submit(engine, action="implementation", request_id="repeat")
        self.assertFalse(
            object_value(
                self.finished(engine, text_value(repeated["action_id"]))["result"]
            )["committed"]
        )

    def test_waiting_cancellation_skips_handler_and_active_cancellation_finishes(
        self: Self,
    ) -> None:
        second = self.sibling()
        entered, release = threading.Event(), threading.Event()
        original = action_commit_desing.run
        calls = []

        def blocked(
            handler: action_commit_desing,
            config: ActionConfig,
            execute: Callable[[ExecutionCommand], int],
            output: Path,
        ) -> JsonObject:
            calls.append(config["task_path"])
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test did not release Git worker")
            return original(handler, config, execute, output)

        with patch.object(action_commit_desing, "run", blocked):
            engine = self.make_engine(workers=True)
            try:
                first = self.submit(engine)
                self.assertTrue(entered.wait(3))
                waiting = self.submit(engine, second)
                for task, receipt in ((self.task_directory, first), (second, waiting)):
                    engine.cancel(
                        task.parent.name,
                        task.name,
                        {"request_id": "cancel", "command_id": receipt["command_id"]},
                    )
                release.set()
                self.assertEqual(
                    self.done(
                        engine, self.task_directory, text_value(first["action_id"])
                    )["outcome"],
                    "succeeded",
                )
                self.assertEqual(
                    self.done(engine, second, text_value(waiting["action_id"]))[
                        "outcome"
                    ],
                    "cancelled",
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    engine.state("set-one", "task-one")["commands"][
                        first["command_id"]
                    ]["outcome"],
                    "cancelled",
                )
            finally:
                release.set()
                engine.close()

    def test_shutdown_drains_queue_and_publishes_completions(self: Self) -> None:
        second = self.sibling()
        entered, release = threading.Event(), threading.Event()
        original = action_commit_desing.run

        def blocked(
            handler: action_commit_desing,
            config: ActionConfig,
            execute: Callable[[ExecutionCommand], int],
            output: Path,
        ) -> JsonObject:
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test did not release Git worker")
            return original(handler, config, execute, output)

        with patch.object(action_commit_desing, "run", blocked):
            engine = self.make_engine(workers=True)
            first = self.submit(engine)
            self.assertTrue(entered.wait(3))
            waiting = self.submit(engine, second)
            with ThreadPoolExecutor(max_workers=1) as pool:
                closing = pool.submit(engine.close)
                self.wait_for(lambda: engine._stopping)
                self.assertFalse(closing.done())
                release.set()
                closing.result(timeout=10)
            for task, receipt in ((self.task_directory, first), (second, waiting)):
                state = engine.state(task.parent.name, task.name)
                self.assertEqual(
                    state["actions"][text_value(receipt["action_id"])]["outcome"],
                    "succeeded",
                )
                self.assertEqual(
                    state["commands"][receipt["command_id"]]["outcome"], "succeeded"
                )
            thread = engine._runner.git_worker.thread
            assert thread is not None
            self.assertFalse(thread.is_alive())

    def crash(self: Self, code: str, action: str = "design") -> None:
        script = (
            """from __future__ import annotations
import os, sys, time
from typing import Self, Unpack
from engine.type_defs import Event, ExportJournal, GitCheckpoint, PathLike, TaskKey
from pathlib import Path
from engine.core import Engine
from engine.actions.common import design_sources
from engine.git_worker import GitWorker
from engine.git_worker.transactions import Transaction
from engine.storage import EventStore
import engine.git_worker.export as export_module
"""
            + code
            + """
root, config = Path(sys.argv[1]), Path(sys.argv[2])
engine = Engine(root, actions_file=config)
task = root / "set-one" / "task-one"
engine.command("set-one", "task-one", {"request_id": "crashed", "action": sys.argv[3], "parameters": {"source": {**design_sources(task), "oracle_passed": True}}})
time.sleep(15)
os._exit(99)
"""
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.root), str(self.config), action],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 88, result.stderr)

    def test_restart_reconstructs_start_without_enqueue(self: Self) -> None:
        self.crash("GitWorker.enqueue = lambda *args: os._exit(88)")
        engine = self.make_engine(workers=True)
        state = engine.state("set-one", "task-one")
        action_id = next(iter(state["actions"]))
        self.assertEqual(self.finished(engine, action_id)["outcome"], "succeeded")
        self.assertEqual(git(self.root, "rev-list", "--count", "HEAD"), "2")

    def test_restart_after_staging_or_commit_never_duplicates_commit(
        self: Self,
    ) -> None:
        for phase in ("staged", "committed"):
            with self.subTest(phase=phase):
                (self.task_directory / "design.md").write_text(f"# {phase}\n")
                self.crash(f"""checkpoint = Transaction.checkpoint
def crash_checkpoint(self: Transaction, phase: str, **values: Unpack[GitCheckpoint]) -> None:
    if phase == {phase!r}:
        os._exit(88)
    return checkpoint(self, phase, **values)
Transaction.checkpoint = crash_checkpoint""")
                engine = self.make_engine(workers=True)
                action_id = engine.state("set-one", "task-one")["current_actions"][
                    "design"
                ]
                action = self.finished(engine, action_id)
                self.assertEqual(action["outcome"], "succeeded", action["error"])
                body = git(
                    self.root,
                    "show",
                    "-s",
                    "--format=%B",
                    text_value(object_value(action["result"])["commit"]),
                )
                self.assertIn(f"Synthetic-Action-ID: {action_id}", body)
                engine.close()
                # Give the next iteration a fresh request namespace and queue.
                self.root = self.base / ("tasks-" + phase)
                self.root.mkdir()
                self.task_directory = self.root / "set-one" / "task-one"
                self.task_directory.mkdir(parents=True)
                (self.task_directory.parent / "template.md").write_text("# Template\n")
                (self.task_directory / "design.md").write_text("# Design\n")
                self.review = self.task_directory / ".review"
                task_repository(self.root)

    def test_restart_after_result_before_event_reuses_saved_result(self: Self) -> None:
        self.crash("""append = EventStore.append
def crash_append(self: EventStore, key: TaskKey, event: Event) -> None:
    if event["type"] == "completed":
        os._exit(88)
    return append(self, key, event)
EventStore.append = crash_append""")
        engine = self.make_engine(workers=True)
        state = engine.state("set-one", "task-one")
        action_id = next(iter(state["actions"]))
        self.assertEqual(self.finished(engine, action_id)["outcome"], "succeeded")
        self.assertEqual(git(self.root, "rev-list", "--count", "HEAD"), "2")
        events = engine.events("set-one", "task-one")["events"]
        self.assertEqual(
            [e["type"] for e in events],
            ["command_started", "started", "completed", "command_completed"],
        )

    def test_export_uses_same_thread_and_recovers_after_push(self: Self) -> None:
        checkout, remote = export_repository(self.base)
        (self.task_directory / "task.toml").write_text(
            '[task]\nname = "exported-task"\n'
        )
        (self.task_directory / "run.sh").write_text("exit 0\n")
        self.set_actions(
            {
                "export": {
                    "handler": "action_export_to_codimango",
                    "runner": {"repo_path": str(checkout)},
                }
            }
        )
        self.crash(
            """write = export_module.atomic_json_write
def crash_verified(file: Path, value: ExportJournal) -> None:
    if value.get("phase") == "verified":
        os._exit(88)
    return write(file, value)
export_module.atomic_json_write = crash_verified""",
            action="export",
        )
        before = git(remote, "rev-parse", "main")
        threads = []
        original = action_export_to_codimango.run

        def record(
            handler: action_export_to_codimango,
            config: ActionConfig,
            execute: Callable[[ExecutionCommand], int],
            output: Path,
        ) -> JsonObject:
            threads.append(threading.current_thread().name)
            return original(handler, config, execute, output)

        with patch.object(action_export_to_codimango, "run", record):
            engine = self.make_engine(workers=True)
            action_id = next(iter(engine.state("set-one", "task-one")["actions"]))
            action = self.finished(engine, action_id)
        self.assertEqual(action["outcome"], "succeeded", action["error"])
        self.assertEqual(threads, ["git-worker"])
        self.assertEqual(git(remote, "rev-parse", "main"), before)
        self.assertEqual(git(checkout, "rev-list", "--count", "HEAD"), "2")

    def test_export_recovers_an_interrupted_destination_install(self: Self) -> None:
        checkout, remote = export_repository(self.base)
        (self.task_directory / "task.toml").write_text(
            '[task]\nname = "exported-task"\n'
        )
        (self.task_directory / "run.sh").write_text("exit 0\n")
        self.set_actions(
            {
                "export": {
                    "handler": "action_export_to_codimango",
                    "runner": {"repo_path": str(checkout)},
                }
            }
        )
        self.crash(
            """replace = export_module.os.replace
def crash_install(source: PathLike, destination: PathLike) -> None:
    if Path(source).name.startswith(".codimango-stage-"):
        os._exit(88)
    return replace(source, destination)
export_module.os.replace = crash_install""",
            action="export",
        )
        engine = self.make_engine(workers=True)
        action_id = next(iter(engine.state("set-one", "task-one")["actions"]))
        action = self.finished(engine, action_id)
        self.assertEqual(action["outcome"], "succeeded", action["error"])
        self.assertEqual(git(remote, "show", "main:exported-task/run.sh"), "exit 0")
        self.assertEqual(list(checkout.glob(".codimango-stage-*")), [])

    def test_failed_request_does_not_stop_later_git_requests(self: Self) -> None:
        second = self.sibling()
        engine = self.make_engine(workers=True)
        source = design_sources(self.task_directory)
        (self.task_directory / "design.md").write_text("# Changed since approval\n")
        rejected = engine.command(
            "set-one",
            "task-one",
            {"request_id": "bad", "action": "design", "parameters": {"source": source}},
        )
        accepted = self.submit(engine, second)
        self.assertEqual(
            self.finished(engine, text_value(rejected["action_id"]))["outcome"],
            "failed",
        )
        self.assertEqual(
            self.done(engine, second, text_value(accepted["action_id"]))["outcome"],
            "succeeded",
        )

    def test_queue_persistence_failure_stops_mutations_and_reconstructs_request(
        self: Self,
    ) -> None:
        from engine.storage import atomic_json_write

        engine = self.make_engine(workers=True)

        def fail_queue(file: Path, value: object) -> None:
            if file.name == "git-queue.json":
                raise OSError("simulated disk failure")
            return atomic_json_write(file, value)

        with (
            patch("engine.git_worker.worker.atomic_json_write", side_effect=fail_queue),
            self.assertRaises(EngineError) as caught,
        ):
            self.submit(engine)
        self.assertEqual(caught.exception.status, 503)
        self.assertTrue(engine._unavailable)
        engine.close()
        self.assertEqual(git(self.root, "rev-list", "--count", "HEAD"), "1")
        restored = self.make_engine(workers=True)
        action_id = next(iter(restored.state("set-one", "task-one")["actions"]))
        self.assertEqual(self.finished(restored, action_id)["outcome"], "succeeded")

    def test_corrupt_queue_stops_startup_without_rewriting_it(self: Self) -> None:
        engine = self.make_engine(workers=True)
        receipt = self.submit(engine)
        self.assertEqual(
            self.finished(engine, text_value(receipt["action_id"]))["outcome"],
            "succeeded",
        )
        engine.close()
        queue = self.root / ".review" / "git-queue.json"
        value = json.loads(queue.read_text())
        value["next_sequence"] = 0
        broken = json.dumps(value)
        queue.write_text(broken)
        with self.assertRaises(CorruptLogError):
            Engine(self.root, actions_file=self.config)
        self.assertEqual(queue.read_text(), broken)
