import contextlib
import io
import json
import sys
from unittest import mock

from engine.actions import load_actions, run_action
from engine.actions.action_commit_desing import action_commit_desing
from engine.actions.common import design_sources
from engine.cli import main as cli_main
from engine.errors import EngineError
from engine.tests.support import EngineFixture
from engine.tests import test_runner


class ActionTests(EngineFixture):
    wait_for = test_runner.RunnerTests.wait_for
    finished = test_runner.RunnerTests.finished
    clean_workers = test_runner.RunnerTests.clean_workers

    def setUp(self):
        super().setUp()
        self.addCleanup(self.clean_workers)
        # Control the sync result for lifecycle tests; Git commits are covered separately.
        sync_run = mock.patch.object(action_commit_desing, "run", return_value={"approved": True})
        sync_run.start()
        self.addCleanup(sync_run.stop)
        self.approval = {"dependencies": [], "handler": "action_commit_desing"}
        self.set_actions({"approve": self.approval})

    def test_sync_completion_contains_result_and_retries_replay_without_execution(self):
        engine = self.make_engine()
        receipt = self.command(engine, "approve", request_id="sync-once")
        self.assertEqual(receipt["type"], "completed")
        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertTrue(receipt["result"]["approved"])
        engine._runner.initiate.assert_not_called()
        state = engine.state("set-one", "task-one")
        self.assertEqual(state["revision"], 1)
        self.assertIsNone(state["actions"][receipt["action_id"]]["started_at"])
        self.assertFalse((self.review / "jobs").exists())
        self.assertEqual(engine.output("set-one", "task-one", receipt["action_id"])["output"], "")
        engine.close()
        restored = self.make_engine()
        with mock.patch.object(action_commit_desing, "run", side_effect=AssertionError("reran")):
            self.assertEqual(self.command(restored, "approve", request_id="sync-once"), receipt)
        self.assertEqual(restored.state("set-one", "task-one"), state)
        with self.assertRaises(EngineError) as caught:
            self.command(restored, "approve", request_id="sync-once", parameters={"different": True})
        self.assertEqual(caught.exception.code, "request_id_conflict")

    def test_sync_failure_is_a_single_completion_and_retry_can_unblock_dependents(self):
        self.set_actions({"approve": self.approval, "process": {"dependencies": ["approve"]}})
        engine = self.make_engine()
        with mock.patch.object(action_commit_desing, "run", side_effect=RuntimeError("approval failed")):
            receipt = self.command(engine, "approve")
        self.assertEqual((receipt["type"], receipt["outcome"]), ("completed", "failed"))
        self.assertIsNone(receipt["result"])
        self.assertIn("approval failed", receipt["error"])
        events = engine.events("set-one", "task-one")["events"]
        self.assertEqual([event["type"] for event in events], ["completed"])
        self.assertEqual(events[0]["payload"]["error"], receipt["error"])
        process = self.command(engine, "process")["action_id"]
        self.assertEqual(engine.state("set-one", "task-one")["actions"][process]["blocked_by"], ["approve"])
        approval = self.command(engine, "approve")["action_id"]
        self.assertEqual(engine.state("set-one", "task-one")["actions"][process]["dependency_actions"], {"approve": approval})
        self.finish(engine, process)
        state = engine.state("set-one", "task-one")
        engine.close()
        self.assertEqual(self.make_engine().state("set-one", "task-one"), state)

    def test_queued_sync_action_resumes_after_restart_and_captures_dependencies(self):
        approval = {**self.approval, "dependencies": ["prepare"]}
        self.set_actions({"prepare": {"dependencies": []}, "approve": approval, "process": {"dependencies": ["approve"]}})
        engine = self.make_engine()
        process = self.command(engine, "process")["action_id"]
        receipt = self.command(engine, "approve", request_id="queued-sync")
        self.assertEqual(receipt["type"], "waiting")
        engine.close()
        self.set_actions({"prepare": {"dependencies": []}, "approve": {"dependencies": ["prepare"]}, "process": {"dependencies": ["approve"]}})
        restored = self.make_engine()
        prepare = self.command(restored, "prepare")["action_id"]
        self.finish(restored, prepare)
        state = restored.state("set-one", "task-one")
        approval_state = state["actions"][receipt["action_id"]]
        self.assertEqual(approval_state["outcome"], "succeeded")
        self.assertEqual(approval_state["execution"]["handler"], "action_commit_desing")
        self.assertTrue(approval_state["result"]["approved"])
        self.assertIsNone(approval_state["started_at"])
        self.assertEqual(approval_state["dependency_actions"], {"prepare": prepare})
        self.assertEqual(state["actions"][process]["dependency_actions"], {"approve": receipt["action_id"]})
        events = [event for event in restored.events("set-one", "task-one")["events"] if event["action_id"] == receipt["action_id"]]
        self.assertEqual([event["type"] for event in events], ["waiting", "completed"])
        self.assertEqual(self.command(restored, "approve", request_id="queued-sync"), receipt)

    def test_sync_uncertain_append_replays_its_original_result(self):
        engine = self.make_engine()
        append = engine._store.append

        def fail_after_append(key, event):
            append(key, event)
            raise OSError("uncertain acknowledgment")

        with mock.patch.object(engine._store, "append", side_effect=fail_after_append):
            with self.assertLogs("engine.core", level="ERROR"), self.assertRaises(EngineError):
                self.command(engine, "approve", request_id="uncertain-sync")
        engine.close()
        restored = self.make_engine()
        with mock.patch.object(action_commit_desing, "run", side_effect=AssertionError("reran")):
            receipt = self.command(restored, "approve", request_id="uncertain-sync")
        self.assertTrue(receipt["result"]["approved"])
        self.assertEqual(restored.state("set-one", "task-one")["revision"], 1)

    def test_sync_handler_keeps_its_captured_async_lifecycle(self):
        for mode in (None, "durable"):
            with self.subTest(mode=mode):
                request_id = f"old-approval-{mode}"
                engine = self.make_engine()
                receipt = self.legacy_command(engine, "approve", request_id=request_id, mode=mode)
                engine.close()
                restored = self.make_engine()
                action = restored.state("set-one", "task-one")["actions"][receipt["action_id"]]
                self.assertEqual((action["mode"], action["status"]), ("async", "started"))
                restored._runner.initiate.assert_called_once()
                self.finish(restored, receipt["action_id"], result={"approved": True})
                self.assertEqual(self.command(restored, "approve", request_id=request_id), receipt)
                events = [event for event in restored.events("set-one", "task-one")["events"] if event["action_id"] == receipt["action_id"]]
                self.assertEqual([event["type"] for event in events], ["waiting", "started", "completed"])
                self.assertEqual(events[0]["payload"].get("mode"), mode)
                restored.close()

    def test_implementation_approval_executes_without_external_commands(self):
        config = {
            "task_path": str(self.task_directory),
            "dependency_results": {
                "implement": {**design_sources(self.task_directory), "oracle_passed": True},
            },
            "execution": {"handler": "action_commit_impl"},
        }

        def execute(_):
            self.fail("Implementation approval must not launch external commands.")

        result = run_action(config, execute, self.base / "output.log")
        self.assertTrue(result["approved"])
        self.assertTrue(result["oracle_passed"])

    def test_async_action_emits_start_then_completion_with_result_or_error(self):
        for code, outcome in ((0, "succeeded"), (7, "failed")):
            with self.subTest(code=code):
                self.set_actions({"run": {"dependencies": [], "mode": "async", "runner": {"argv": [sys.executable, "-c", f"import sys; print('SYNTHETIC_RESULT={{\"value\":42}}'); sys.exit({code})"]}}})
                engine = self.make_engine(workers=True)
                receipt = self.command(engine, "run")
                self.assertEqual(receipt["type"], "started")
                action = self.finished(engine, receipt["action_id"])
                self.assertEqual(action["mode"], "async")
                self.assertEqual(action["outcome"], outcome)
                events = [event for event in engine.events("set-one", "task-one")["events"] if event["action_id"] == receipt["action_id"]]
                self.assertEqual([event["type"] for event in events], ["started", "completed"])
                self.assertEqual(events[0]["payload"]["mode"], "async")
                if code:
                    self.assertIn("status 7", events[-1]["payload"]["error"])
                else:
                    self.assertEqual(events[-1]["payload"]["result"]["value"], 42)
                engine.close()

    def test_cli_prints_sync_result_once_and_reports_failure_exit_status(self):
        for outcome, expected_status in (("succeeded", 0), ("failed", 1)):
            receipt = {"action_id": "fixture", "type": "completed", "outcome": outcome, "result": {"approved": True} if expected_status == 0 else None, "error": "bad input" if expected_status else None}
            output = io.StringIO()
            with mock.patch("engine.cli.request", return_value=receipt) as request, contextlib.redirect_stdout(output):
                status = cli_main(["run", "set-one", "task-one", "approve", "--wait"])
            self.assertEqual(status, expected_status)
            self.assertEqual(json.loads(output.getvalue()), receipt)
            request.assert_called_once()

    def test_cli_prints_async_start_and_completion(self):
        receipt = {"action_id": "fixture", "type": "started"}
        action = {
            "action_id": "fixture", "status": "completed", "outcome": "succeeded",
            "result": {"value": 42}, "error": None,
        }
        output = io.StringIO()
        with mock.patch("engine.cli.request", side_effect=[receipt, {"actions": {"fixture": action}}]):
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli_main(["run", "set-one", "task-one", "run", "--wait"]), 0)
        text = output.getvalue().strip()
        decoder = json.JSONDecoder()
        records = []
        while text:
            record, offset = decoder.raw_decode(text)
            records.append(record)
            text = text[offset:].lstrip()
        self.assertEqual([record["type"] for record in records], ["started", "completed"])
        self.assertEqual(records[-1]["result"], {"value": 42})

    def test_config_rejects_unknown_modes_and_mode_handler_mismatch(self):
        for mode in ("background", [], "durable", "async"):
            self.set_actions({"approve": {**self.approval, "mode": mode}})
            with self.assertRaises(EngineError):
                load_actions(self.config)

        self.set_actions({"run": {"dependencies": [], "mode": "durable"}})
        with self.assertRaisesRegex(EngineError, "mode must be sync or async"):
            load_actions(self.config)

    def test_config_rejects_invalid_handler_references(self):
        for handler in (None, [], "commit_desing", "action_missing", "../action_commit_desing"):
            with self.subTest(handler=handler):
                self.set_actions({"approve": {**self.approval, "handler": handler}})
                with self.assertRaises(EngineError):
                    load_actions(self.config)

    def test_config_rejects_invalid_handler_settings(self):
        for handler, runner in (
            ("action_commit_desing", {"argv": ["python3"]}),
            ("action_command", {}),
            ("action_command", {"argv": ["python3"], "timeout_ms": False}),
            ("action_export_to_codimango", {}),
            ("action_export_to_codimango", {"repo_path": ""}),
            ("action_export_to_codimango", {"repo_path": []}),
        ):
            with self.subTest(handler=handler, runner=runner):
                self.set_actions({"run": {"dependencies": [], "handler": handler, "runner": runner}})
                with self.assertRaises(EngineError):
                    load_actions(self.config)
