from __future__ import annotations

import contextlib
import io
import json
import sys
from typing import Self, cast
from unittest import mock
from unittest.mock import Mock

from engine.actions import load_actions
from engine.cli import main as cli_main
from engine.errors import EngineError
from engine.json_data import object_value, text_value
from engine.tests.support import WorkerFixture
from engine.type_defs import JsonObject, JsonValue, RequestBody


class ActionTests(WorkerFixture):
    def setUp(self: Self) -> None:
        super().setUp()
        self.addCleanup(self.clean_workers)

    def test_historical_mode_metadata_does_not_change_worker_lifecycle(
        self: Self,
    ) -> None:
        for mode in (None, "durable", "async"):
            with self.subTest(mode=mode):
                request_id = f"old-action-{mode}"
                engine = self.make_engine()
                receipt = self.legacy_command(
                    engine, "prepare", request_id=request_id, mode=mode
                )
                engine.close()
                restored = self.make_engine()
                action = restored.state("set-one", "task-one")["actions"][
                    text_value(receipt["action_id"])
                ]
                self.assertEqual(action["status"], "started")
                self.assertNotIn("mode", action)
                cast(Mock, restored._runner.initiate).assert_called_once()
                self.finish(
                    restored, text_value(receipt["action_id"]), result={"value": 42}
                )
                self.assertEqual(
                    self.command(restored, "prepare", request_id=request_id), receipt
                )
                events = [
                    event
                    for event in restored.events("set-one", "task-one")["events"]
                    if event["action_id"] == receipt["action_id"]
                ]
                self.assertEqual(
                    [event["type"] for event in events],
                    ["waiting", "started", "completed"],
                )
                self.assertEqual(events[0]["payload"].get("mode"), mode)
                restored.close()

    def test_action_emits_start_then_completion_with_result_or_error(
        self: Self,
    ) -> None:
        for code, outcome in ((0, "succeeded"), (7, "failed")):
            with self.subTest(code=code):
                self.set_actions(
                    {
                        "run": {
                            "runner": {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    f"import sys; print('SYNTHETIC_RESULT={{\"value\":42}}'); sys.exit({code})",
                                ]
                            }
                        }
                    }
                )
                engine = self.make_engine(workers=True)
                request_id = f"run-{code}"
                receipt = self.command(engine, "run", request_id=request_id)
                self.assertEqual(receipt["type"], "command_started")
                action = self.finished(engine, text_value(receipt["action_id"]))
                self.assertNotIn("mode", action)
                self.assertNotIn("mode", engine.list_actions()["actions"]["run"])
                self.assertEqual(action["outcome"], outcome)
                events = engine.events("set-one", "task-one")["events"]
                self.assertNotIn("mode", events[0]["payload"]["steps"][0])
                execution_events = [
                    event
                    for event in events
                    if event["action_id"] == receipt["action_id"]
                ]
                self.assertEqual(
                    [event["type"] for event in execution_events],
                    ["started", "completed"],
                )
                if code:
                    self.assertIn(
                        "status 7", text_value(execution_events[-1]["payload"]["error"])
                    )
                else:
                    self.assertEqual(
                        object_value(execution_events[-1]["payload"]["result"])[
                            "value"
                        ],
                        42,
                    )
                engine.close()
                restored = self.make_engine()
                self.assertEqual(
                    self.command(restored, "run", request_id=request_id), receipt
                )
                cast(Mock, restored._runner.initiate).assert_not_called()
                restored.close()

    def test_historical_completed_only_action_replays_without_execution(
        self: Self,
    ) -> None:
        engine = self.make_engine()
        request: RequestBody = {
            "request_id": "old-inline",
            "action": "prepare",
            "parameters": {},
        }
        receipt = engine._commit(
            ("set-one", "task-one"),
            request,
            "command",
            "completed",
            "d59d66c3-8f9d-47ac-aa95-2b88842058a3",
            {
                "action": "prepare",
                "parameters": {},
                "dependencies": [],
                "mode": "sync",
                "dependency_actions": {},
                "outcome": "succeeded",
                "result": {"value": 42},
                "error": None,
            },
        )
        state = engine.state("set-one", "task-one")
        engine.close()
        restored = self.make_engine()
        self.assertEqual(restored.state("set-one", "task-one"), state)
        self.assertEqual(
            self.command(restored, "prepare", request_id="old-inline"), receipt
        )
        self.assertNotIn("mode", state["actions"][text_value(receipt["action_id"])])
        cast(Mock, restored._runner.initiate).assert_not_called()

    def test_cli_prints_completed_command_once_and_reports_failure_exit_status(
        self: Self,
    ) -> None:
        for outcome, expected_status in (("succeeded", 0), ("failed", 1)):
            receipt = {
                "command_id": "fixture",
                "type": "command_completed",
                "outcome": outcome,
                "result": {"value": 42} if expected_status == 0 else None,
                "error": "bad input" if expected_status else None,
            }
            output = io.StringIO()
            with (
                mock.patch("engine.cli.request", return_value=receipt) as request,
                contextlib.redirect_stdout(output),
            ):
                status = cli_main(["run", "set-one", "task-one", "prepare", "--wait"])
            self.assertEqual(status, expected_status)
            self.assertEqual(json.loads(output.getvalue()), receipt)
            request.assert_called_once()

    def test_cli_prints_start_and_completion(self: Self) -> None:
        receipt = {"action_id": "fixture", "type": "started"}
        action = {
            "action_id": "fixture",
            "status": "completed",
            "outcome": "succeeded",
            "result": {"value": 42},
            "error": None,
        }
        output = io.StringIO()
        with mock.patch(
            "engine.cli.request",
            side_effect=[receipt, {"actions": {"fixture": action}}],
        ):
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    cli_main(["run", "set-one", "task-one", "run", "--wait"]), 0
                )
        text = output.getvalue().strip()
        decoder = json.JSONDecoder()
        records = []
        while text:
            record, offset = decoder.raw_decode(text)
            records.append(record)
            text = text[offset:].lstrip()
        self.assertEqual(
            [record["type"] for record in records], ["started", "completed"]
        )
        self.assertEqual(records[-1]["result"], {"value": 42})

    def test_config_rejects_removed_mode_field(self: Self) -> None:
        modes: list[JsonValue] = ["sync", "async", "durable", "background", []]
        for mode in modes:
            with self.subTest(mode=mode):
                self.set_actions({"run": {"mode": mode}})
                with self.assertRaises(EngineError):
                    load_actions(self.config)

    def test_config_rejects_invalid_handler_references(self: Self) -> None:
        handlers: list[JsonValue] = [
            None,
            [],
            "commit_desing",
            "action_missing",
            "../action_command",
        ]
        for handler in handlers:
            with self.subTest(handler=handler):
                self.set_actions({"run": {"handler": handler}})
                with self.assertRaises(EngineError):
                    load_actions(self.config)

    def test_config_rejects_invalid_handler_settings(self: Self) -> None:
        cases: list[tuple[str, JsonObject]] = [
            ("action_command", {}),
            ("action_command", {"argv": ["python3"], "timeout_ms": False}),
            ("action_export_to_codimango", {}),
            ("action_export_to_codimango", {"repo_path": ""}),
            ("action_export_to_codimango", {"repo_path": []}),
        ]
        for handler, runner in cases:
            with self.subTest(handler=handler, runner=runner):
                self.set_actions({"run": {"handler": handler, "runner": runner}})
                with self.assertRaises(EngineError):
                    load_actions(self.config)
