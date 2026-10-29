from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Self
from unittest.mock import patch

from engine.json_data import object_value, text_value
from engine.runner import job_directory
from engine.storage import atomic_json_write
from engine.tests.support import HistoricalEngineFixture as EngineFixture
from engine.tests.support import WorkerFixture
from engine.worker import group_exists

if TYPE_CHECKING:
    pass


class RunnerTests(WorkerFixture, EngineFixture):
    def setUp(self: Self) -> None:
        super().setUp()
        self.addCleanup(self.clean_workers)

    def test_executes_dependency_graph_and_literal_parameter_input(self: Self) -> None:
        text = "two lines\n`touch should-not-exist` $(echo nope) {literal}"
        self.set_actions(
            {
                "prepare": self.script(
                    "import sys; print(sys.stdin.read()); print('SYNTHETIC_RESULT={\"value\":42}')",
                    input="{parameters.text}",
                ),
                "process": self.script('print("processed")', dependencies=["prepare"]),
            }
        )
        engine = self.make_engine(workers=True)
        process = text_value(self.command(engine, "process")["action_id"])
        prepare = text_value(
            self.command(engine, "prepare", parameters={"text": text})["action_id"]
        )
        self.assertEqual(self.finished(engine, process)["outcome"], "succeeded")
        self.assertEqual(
            object_value(
                engine.state("set-one", "task-one")["actions"][prepare]["result"]
            )["value"],
            42,
        )
        self.assertIn(text, engine.output("set-one", "task-one", prepare)["output"])
        self.assertFalse((self.task_directory / "should-not-exist").exists())
        self.assertEqual(
            engine.state("set-one", "task-one")["actions"][process][
                "dependency_actions"
            ],
            {"prepare": prepare},
        )

    def test_independent_actions_run_concurrently_within_and_across_tasks(
        self: Self,
    ) -> None:
        gate = self.base / "release"
        block = f"import time; from pathlib import Path; p=Path({str(gate)!r});\nwhile not p.exists(): time.sleep(.05)"
        self.set_actions(
            {
                "one": self.script(block),
                "two": self.script(
                    'from pathlib import Path; Path("two.txt").write_text("two")'
                ),
            }
        )
        other = self.task_directory.with_name("task-two")
        other.mkdir()
        (other / "design.md").write_text("# Other")
        engine = self.make_engine(workers=True)
        first = text_value(self.command(engine, "one")["action_id"])
        second = text_value(self.command(engine, "two")["action_id"])
        self.assertEqual(self.finished(engine, second)["outcome"], "succeeded")
        self.assertEqual((self.task_directory / "two.txt").read_text(), "two")
        independent = text_value(
            engine.command(
                "set-one", "task-two", {"request_id": "independent", "action": "two"}
            )["action_id"]
        )
        self.wait_for(
            lambda: (
                engine.state("set-one", "task-two")["actions"][independent]["outcome"]
                == "succeeded"
            )
        )
        self.assertEqual(
            engine.state("set-one", "task-one")["actions"][first]["status"], "started"
        )
        gate.touch()
        self.assertEqual(self.finished(engine, first)["outcome"], "succeeded")

    def test_older_resource_metadata_does_not_block_replayed_actions(
        self: Self,
    ) -> None:
        from engine.state import empty_state, reduce_event

        self.set_actions(
            {"one": self.script('print("one")'), "two": self.script('print("two")')}
        )
        engine = self.make_engine()
        self.legacy_command(engine, "one")
        engine._start_ready(("set-one", "task-one"))
        second = text_value(self.legacy_command(engine, "two")["action_id"])
        history = engine.events("set-one", "task-one")["events"][:3]
        state = empty_state(("set-one", "task-one"))
        for event in history:
            if event["type"] == "waiting":
                event["payload"]["resources"] = ["task_files"]
            state = reduce_event(state, event)
        self.assertIn(second, state["ready_action_ids"])
        self.assertNotIn("resources", state["actions"][second])
        self.assertNotIn("blocked_resources", state["actions"][second])

    def test_restart_reconnects_to_live_worker_and_applies_offline_result_once(
        self: Self,
    ) -> None:
        gate, count = self.base / "release", self.base / "count"
        script = f'from pathlib import Path; import time; Path({str(count)!r}).open("a").write("launch\\n");\nwhile not Path({str(gate)!r}).exists(): time.sleep(.05)\nprint("done")'
        self.set_actions({"run": self.script(script)})
        engine = self.make_engine(workers=True)
        receipt = self.command(engine, "run", request_id="stable")
        self.wait_for(count.exists)
        engine.close()
        restored = self.make_engine(workers=True)
        self.assertEqual(self.command(restored, "run", request_id="stable"), receipt)
        self.assertEqual(
            restored.state("set-one", "task-one")["actions"][
                text_value(receipt["action_id"])
            ]["status"],
            "started",
        )
        restored.close()
        gate.touch()
        directory = job_directory(
            self.root, ("set-one", "task-one"), text_value(receipt["action_id"])
        )
        self.wait_for(lambda: (directory / "result.json").exists())
        final = self.make_engine(workers=True)
        self.assertEqual(
            self.finished(final, text_value(receipt["action_id"]))["outcome"],
            "succeeded",
        )
        self.assertEqual(count.read_text(), "launch\n")
        self.assertEqual(final.state("set-one", "task-one")["revision"], 2)

    def test_configuration_is_captured_for_a_waiting_command(self: Self) -> None:
        self.set_actions(
            {
                "prepare": self.script('print("prepared")'),
                "process": self.script('print("old runner")', dependencies=["prepare"]),
            }
        )
        engine = self.make_engine(workers=True)
        process = text_value(self.command(engine, "process")["action_id"])
        engine.close()
        self.set_actions(
            {
                "prepare": self.script('print("prepared")'),
                "process": self.script(
                    'raise RuntimeError("new runner")', dependencies=["prepare"]
                ),
            }
        )
        restored = self.make_engine(workers=True)
        self.command(restored, "prepare")
        self.assertEqual(self.finished(restored, process)["outcome"], "succeeded")
        self.assertIn(
            "old runner", restored.output("set-one", "task-one", process)["output"]
        )

    def test_cancel_waiting_and_running_actions_and_replay(self: Self) -> None:
        self.set_actions(
            {
                "prepare": self.script("import time; time.sleep(30)"),
                "process": self.script('print("unused")', dependencies=["prepare"]),
            }
        )
        engine = self.make_engine(workers=True)
        waiting = text_value(self.command(engine, "process")["action_id"])
        cancel = {"request_id": "cancel-wait", "action_id": waiting}
        receipt = engine.cancel("set-one", "task-one", cancel)
        self.assertEqual(engine.cancel("set-one", "task-one", cancel), receipt)
        self.assertEqual(
            engine.state("set-one", "task-one")["actions"][waiting]["outcome"],
            "cancelled",
        )
        running = text_value(self.command(engine, "prepare")["action_id"])
        directory = job_directory(self.root, ("set-one", "task-one"), running)
        self.wait_for(lambda: (directory / "process.json").exists())
        group = json.loads((directory / "process.json").read_text())["pgid"]
        engine.cancel(
            "set-one", "task-one", {"request_id": "cancel-run", "action_id": running}
        )
        self.assertEqual(self.finished(engine, running)["outcome"], "cancelled")
        self.assertFalse(group_exists(group))
        engine.close()
        restored = self.make_engine(workers=True)
        self.assertEqual(
            restored.state("set-one", "task-one")["actions"][running]["outcome"],
            "cancelled",
        )

    def test_timeout_and_missing_success_marker_fail(self: Self) -> None:
        self.set_actions(
            {
                "timeout": self.script("import time; time.sleep(30)", timeout_ms=100),
                "marker": self.script(
                    'print("success")', success_result={"oracle_passed": True}
                ),
            }
        )
        engine = self.make_engine(workers=True)
        timed = text_value(self.command(engine, "timeout")["action_id"])
        marker = text_value(self.command(engine, "marker")["action_id"])
        self.assertEqual(self.finished(engine, timed)["outcome"], "timed_out")
        self.assertEqual(self.finished(engine, marker)["outcome"], "failed")

    def test_interrupted_worker_fails_without_relaunch_and_removes_child(
        self: Self,
    ) -> None:
        self.set_actions({"run": self.script("import time; time.sleep(30)")})
        engine = self.make_engine(workers=True)
        running = text_value(self.command(engine, "run")["action_id"])
        directory = job_directory(self.root, ("set-one", "task-one"), running)
        self.wait_for(lambda: (directory / "process.json").exists())
        worker = json.loads((directory / "worker.json").read_text())
        group = json.loads((directory / "process.json").read_text())["pgid"]
        os.kill(worker["pid"], signal.SIGKILL)
        self.assertEqual(self.finished(engine, running)["outcome"], "failed")
        self.wait_for(lambda: not group_exists(group))
        engine.close()
        restored = self.make_engine(workers=True)
        self.assertEqual(restored.state("set-one", "task-one")["revision"], 2)

    def test_launch_receipt_failure_stops_the_untracked_worker(self: Self) -> None:
        from engine import runner

        self.set_actions({"run": self.script("import time; time.sleep(30)")})
        engine = self.make_engine(workers=True)
        write = atomic_json_write

        def fail_receipt(file: Path, value: object) -> None:
            if file.name == "launch.json":
                raise OSError("receipt failure")
            return write(file, value)

        with patch.object(runner, "atomic_json_write", side_effect=fail_receipt):
            running = text_value(self.command(engine, "run")["action_id"])
        self.assertEqual(self.finished(engine, running)["outcome"], "failed")
        self.assertTrue(
            all(child.poll() is not None for child in engine._runner.children)
        )

    def test_restart_does_not_launch_a_persisted_start_without_a_worker_record(
        self: Self,
    ) -> None:
        marker = self.base / "unexpected-launch"
        self.set_actions(
            {
                "run": self.script(
                    f"from pathlib import Path; Path({str(marker)!r}).touch()"
                )
            }
        )
        engine = self.make_engine()
        running = text_value(self.command(engine, "run")["action_id"])
        engine.close()
        restored = self.make_engine(workers=True)
        self.assertEqual(self.finished(restored, running)["outcome"], "failed")
        self.assertFalse(marker.exists())

    def test_saved_cancel_is_reapplied_after_restart(self: Self) -> None:
        self.set_actions({"run": self.script("import time; time.sleep(30)")})
        engine = self.make_engine(workers=True)
        running = text_value(self.command(engine, "run")["action_id"])
        directory = job_directory(self.root, ("set-one", "task-one"), running)
        self.wait_for(lambda: (directory / "process.json").exists())
        with patch.object(
            engine._runner, "cancel", side_effect=OSError("cancel signal failed")
        ):
            with self.assertRaises(OSError):
                engine.cancel(
                    "set-one",
                    "task-one",
                    {"request_id": "cancel-before-restart", "action_id": running},
                )
            engine.close()
        restored = self.make_engine(workers=True)
        self.assertEqual(self.finished(restored, running)["outcome"], "cancelled")

    def test_restart_fails_legacy_start_without_execution(self: Self) -> None:
        engine = self.make_engine()
        running = text_value(self.legacy_command(engine, "prepare")["action_id"])
        engine._start_ready(("set-one", "task-one"))
        engine.close()
        log = self.review / "events.jsonl"
        events = [json.loads(line) for line in log.read_text().splitlines()]
        events[0]["payload"].pop("execution")
        log.write_text("".join(json.dumps(event) + "\n" for event in events))

        restored = self.make_engine(workers=True)
        action = self.finished(restored, running)
        self.assertEqual(action["outcome"], "failed")
        self.assertIn("Submit a new command to retry", text_value(action["error"]))
        self.assertEqual(restored.state("set-one", "task-one")["revision"], 3)

    def test_saved_result_without_optional_fields_reconciles(self: Self) -> None:
        engine = self.make_engine()
        receipt = self.command(engine, "prepare")
        identity = receipt["action_id"]
        key = ("set-one", "task-one")
        directory = job_directory(self.root, key, identity, create=True)
        atomic_json_write(directory / "result.json", {"outcome": "succeeded"})
        with engine._lock:
            completion = engine._runner._completion(
                key, engine._states[key]["actions"][identity]
            )
            assert completion is not None
            engine._runner._apply(key, identity, completion)
        action = engine.state(*key)["actions"][identity]
        self.assertEqual(action["outcome"], "succeeded")
        self.assertIsNone(action["result"])
        self.assertIsNone(action["error"])

    def test_output_offsets_preserve_utf8_boundaries(self: Self) -> None:
        self.set_actions({"run": self.script('print("unused")')})
        engine = self.make_engine()
        action = text_value(self.command(engine, "run")["action_id"])
        directory = job_directory(
            self.root, ("set-one", "task-one"), action, create=True
        )
        file = directory / "output.log"
        file.write_bytes(b"a" * (256 * 1024 - 1) + "€end".encode())
        first = engine.output("set-one", "task-one", action)
        second = engine.output("set-one", "task-one", action, after=first["next"])
        self.assertEqual(first["output"] + second["output"], file.read_text())

    def test_dependency_updates_make_existing_dependent_results_stale(
        self: Self,
    ) -> None:
        self.set_actions(
            {
                "redesign": {"dependencies": []},
                "approve": {"dependencies": ["redesign"]},
                "impl": {"dependencies": ["approve"]},
                "custom": {"dependencies": []},
            }
        )
        engine = self.make_engine()
        self.complete_action(engine, "redesign")
        approval = self.complete_action(engine, "approve")
        implementation = self.complete_action(engine, "impl")
        self.complete_action(engine, "custom")
        state = engine.state("set-one", "task-one")
        self.assertFalse(state["actions"][approval]["stale"])
        self.assertFalse(state["actions"][implementation]["stale"])
        self.complete_action(engine, "redesign")
        state = engine.state("set-one", "task-one")
        self.assertTrue(state["actions"][approval]["stale"])
        self.assertTrue(state["actions"][implementation]["stale"])
        next_approval = self.complete_action(engine, "approve")
        self.assertFalse(
            engine.state("set-one", "task-one")["actions"][next_approval]["stale"]
        )
