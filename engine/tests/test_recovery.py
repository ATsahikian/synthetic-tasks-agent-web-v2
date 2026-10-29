"""Agent diagnostics, durable replacement attempts, and historical compatibility."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Self, cast
from unittest.mock import Mock, patch

from engine.actions import fingerprint
from engine.actions.common import digest
from engine.actions.recovery import classify_error, terminal_error
from engine.cli import main as cli_main
from engine.core import ENGINE_DIRECTORY, Engine
from engine.errors import CorruptLogError, EngineError
from engine.json_data import array_value, object_value
from engine.runner import action_configuration, job_directory
from engine.tests.support import WorkerFixture
from engine.type_defs import (
    CommandDefinition,
    ErrorClassification,
    Event,
    FailureInfo,
    JsonObject,
    Receipt,
    TaskKey,
)

REFUSAL = "ERROR: This content was flagged for possible cybersecurity risk. Join the Trusted Access for Cyber program."
RATE_LIMIT = "HTTP 429: Too Many Requests"


class RecoveryTests(WorkerFixture):
    def setUp(self: Self) -> None:
        super().setUp()
        self.addCleanup(self.clean_workers)
        self.config.write_text((ENGINE_DIRECTORY / "actions/actions.json").read_text())
        guide = self.base / "guide.md"
        guide.write_text("Frozen guide {task_name}")
        self.guide = guide
        self.clock = datetime(2026, 10, 29, tzinfo=timezone.utc)
        clock = patch.object(Engine, "_now", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.binary = self.base / "agent"
        self.binary.write_text(
            f"#!{sys.executable}\n"
            + r"""
import json, os, re, sys
from pathlib import Path
prompt = sys.stdin.read() if '--workspace' not in sys.argv else sys.argv[-1]
agent = os.environ['SYNTHETIC_AGENT']
behavior = json.loads(Path(os.environ['RECOVERY_FIXTURE_BEHAVIOR']).read_text())
mode = behavior.get(agent, 'success')
with Path(os.environ['RECOVERY_FIXTURE_LOG']).open('a') as log:
    log.write(json.dumps({'agent': agent, 'id': os.environ['SYNTHETIC_JOB_ID'], 'argv': sys.argv, 'prompt': prompt, 'disable_1m': os.environ.get('CLAUDE_CODE_DISABLE_1M_CONTEXT')}) + '\n')
if mode in ('refusal', 'zero_refusal', 'zero_rate', 'unknown'):
    text = 'ERROR: This content was flagged for possible cybersecurity risk. Join the Trusted Access for Cyber program.' if 'refusal' in mode else 'HTTP 429: Too Many Requests' if mode == 'zero_rate' else 'unexpected provider failure'
    print(json.dumps({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'errors': [text]}))
    sys.exit(0 if mode.startswith('zero') else 1)
if mode == 'intermediate_rate':
    print('ERROR: Rate limit reached. Retrying in 2.0s (1/5).')
if 'design.candidate.md' in prompt:
    Path(re.search(r'Write only (.*?/design.candidate.md)\.', prompt)[1]).write_text('# Recovered design\n')
elif '# Implement One Task and Pass Its Oracle' in prompt:
    marker = 'SYNTHETIC_RESULT={"oracle_passed":true}'
    print(json.dumps({'type':'result','is_error':False,'result':marker}) if 'stream-json' in sys.argv else marker)
elif 'analysis.source.json' in prompt:
    report = Path(re.search(r'Write only (.*?/analysis.md)\.', prompt)[1])
    manifest = json.loads((report.parent / 'analysis.source.json').read_text())
    headings = ['Executive summary','Trajectory source','Benchmark calibration','Trial coverage','Cross-trial findings','Difficulty diagnosis','Weak points','Recommendations','Evidence']
    report.write_text('# Trajectory analysis\n'+'\n'.join(headings)+'\n'+'\n'.join(manifest['artifact_sha256']))
elif 'troubleshoot.json' in prompt:
    report = Path(re.search(r'Write only (.*?/troubleshoot.json) as', prompt)[1])
    report.write_text(json.dumps({'schema_version':1,'findings':[{'classification':'undetermined','cause':'Checker failed','evidence':[{'pointer':'/status','quote':'failed'}]}]}))
else:
    print('custom done')
"""
        )
        self.binary.chmod(0o700)
        self.behavior, self.invocations = (
            self.base / "behavior.json",
            self.base / "invocations.jsonl",
        )
        self.behavior.write_text("{}")
        environment = patch.dict(
            "os.environ",
            {
                "CODEX_BIN": str(self.binary),
                "CLAUDE_BIN": str(self.binary),
                "MUSE_BIN": str(self.binary),
                "CYBER_BENCH_GUIDE_PATH": str(guide),
                "RECOVERY_FIXTURE_BEHAVIOR": str(self.behavior),
                "RECOVERY_FIXTURE_LOG": str(self.invocations),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.definitions: dict[str, CommandDefinition] = {
            "one-action": {"handler": "command_one_action"},
            "chain": {
                "handler": "command_one_shot",
                "actions": [
                    {"action": "custom", "dependencies": [], "parameter_bindings": {}},
                    {
                        "action": "consume",
                        "dependencies": ["custom"],
                        "parameter_bindings": {
                            "source": {"step": "custom", "value": "result"},
                            "source_id": {"step": "custom", "value": "action_id"},
                        },
                    },
                    {
                        "action": "redesign",
                        "dependencies": [],
                        "parameter_bindings": {},
                    },
                ],
            },
        }
        catalog = json.loads(self.config.read_text())
        catalog["actions"]["consume"] = {
            "handler": "action_command",
            "runner": {"argv": [sys.executable, "-c", "pass"]},
        }
        self.config.write_text(json.dumps(catalog))

    def engine(self: Self, *, workers: bool = False) -> Engine:
        return self.make_engine(commands=self.definitions, workers=workers)

    def submit(self: Self, engine: Engine, *, chain: bool = False) -> Receipt:
        return engine.command(
            "set-one",
            "task-one",
            {
                "request_id": "recover",
                "command": "chain" if chain else "one-action",
                "parameters": {"prompt": "Frozen request"}
                if chain
                else {
                    "action": "custom",
                    "action_parameters": {"prompt": "Frozen request"},
                },
            },
        )

    def fail_attempt(
        self: Self,
        engine: Engine,
        action_id: str,
        classification: ErrorClassification = "rate_limit",
        *,
        origin: str = "agent",
    ) -> None:
        text = (
            RATE_LIMIT
            if classification == "rate_limit"
            else REFUSAL
            if classification == "cyber_safety"
            else "unexpected failure"
        )
        directory = job_directory(
            self.root, ("set-one", "task-one"), action_id, create=True
        )
        (directory / "output.log").write_text(text)
        failure = cast(
            FailureInfo,
            {
                "classification": classification,
                "origin": origin,
                "diagnostics": text,
                "exit_code": 1,
            },
        )
        self.finish(
            engine, action_id, outcome="failed", error="CLI failure", failure=failure
        )

    def head(self: Self, engine: Engine) -> str:
        state = engine.state("set-one", "task-one")
        active = state["active_command_id"]
        assert active is not None
        return next(
            identity
            for identity in state["commands"][active]["action_ids"]
            if state["actions"][identity]["status"] != "completed"
        )

    def advance_time(self: Self, engine: Engine, seconds: int = 60) -> None:
        self.clock += timedelta(seconds=seconds)
        with engine._lock:
            engine._start_ready(("set-one", "task-one"))

    def test_classifier_v1_fixtures_and_terminal_zero_exit_results(self: Self) -> None:
        for text in (
            "ERROR: Rate limit reached. Retrying in 2.0s (1/5).",
            "rate limit exceeded for upstream model in 60s",
            "HTTP 429: Too Many Requests",
            "RESOURCE_EXHAUSTED: quota temporarily unavailable",
            "rate-limited reached",
            "status code 429",
            "HTTP error 429",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_error(text), "rate_limit")
        self.assertEqual(classify_error(REFUSAL), "cyber_safety")
        self.assertEqual(classify_error(REFUSAL + RATE_LIMIT), "cyber_safety")
        self.assertEqual(classify_error("ordinary error"), "unknown")
        for event in (
            {"type": "result", "is_error": True, "errors": [RATE_LIMIT]},
            {"type": "result", "subtype": "error_during_execution", "result": REFUSAL},
            {"type": "turn.failed", "error": {"message": RATE_LIMIT}},
            {"type": "error", "message": REFUSAL},
        ):
            with self.subTest(event=event):
                self.assertIsNotNone(terminal_error(json.dumps(event)))
                self.assertIsNone(
                    terminal_error(
                        json.dumps(event)
                        + "\n"
                        + json.dumps(
                            {"type": "result", "is_error": False, "result": "done"}
                        )
                    )
                )
        self.assertIsNone(
            terminal_error("ERROR: Rate limit reached. Retrying\ncustom done\n")
        )
        self.assertIsNone(
            terminal_error(
                json.dumps({"type": "assistant", "message": {"content": REFUSAL}})
            )
        )

    def test_three_total_attempts_per_model_then_exhaustion(self: Self) -> None:
        engine = self.engine()
        receipt = self.submit(engine, chain=True)
        attempts: list[tuple[str, int]] = []
        for index in range(9):
            identity = self.head(engine)
            action = engine.state("set-one", "task-one")["actions"][identity]
            assert action["execution"] is not None
            attempts.append((action["execution"]["agent"], action["attempt_number"]))
            self.fail_attempt(engine, identity)
            if index < 8:
                replacement = engine.state("set-one", "task-one")["actions"][
                    self.head(engine)
                ]
                if index % 3 != 2:
                    self.assertEqual(replacement["status"], "queued")
                    self.assertEqual(
                        datetime.fromisoformat(replacement["execute_at"]) - self.clock,
                        timedelta(seconds=60),
                    )
                    self.advance_time(engine, 59)
                    self.assertEqual(
                        engine.state("set-one", "task-one")["actions"][
                            replacement["action_id"]
                        ]["status"],
                        "queued",
                    )
                    self.advance_time(engine, 1)
                else:
                    self.assertEqual(replacement["status"], "started")
        self.assertEqual(
            attempts,
            [
                (agent, number)
                for agent in ("codex", "claude", "muse")
                for number in (1, 2, 3)
            ],
        )
        state = engine.state("set-one", "task-one")
        command = state["commands"][receipt["command_id"]]
        self.assertEqual(command["outcome"], "failed")
        self.assertIn("exhausted Muse", command["error"] or "")
        self.assertEqual(state["action_pool"], [])
        self.assertEqual(
            [
                state["actions"][identity]["outcome"]
                for identity in receipt["action_ids"][1:]
            ],
            ["cancelled", "cancelled"],
        )
        events = engine.events("set-one", "task-one")["events"]
        self.assertEqual(sum(event["type"] == "retry_scheduled" for event in events), 8)
        self.assertEqual(
            sum(event["type"] == "recovery_exhausted" for event in events), 1
        )
        self.assertTrue(all(event["schema_version"] == 3 for event in events))
        self.assertEqual(cast(Mock, engine._runner.initiate).call_count, 9)

    def test_mixed_errors_bind_successful_replacement_and_start_next_agent_step_with_codex(
        self: Self,
    ) -> None:
        engine = self.engine()
        receipt = self.submit(engine, chain=True)
        original = engine.state("set-one", "task-one")["commands"][
            receipt["command_id"]
        ]["steps"]
        first = self.head(engine)
        self.fail_attempt(engine, first)
        second = self.head(engine)
        self.advance_time(engine)
        self.fail_attempt(engine, second, "cyber_safety")
        third = self.head(engine)
        self.assertEqual(
            engine.state("set-one", "task-one")["actions"][third]["attempt_number"], 1
        )
        self.fail_attempt(engine, third)
        self.advance_time(engine)
        fourth = self.head(engine)
        self.fail_attempt(engine, fourth, "cyber_safety")
        successful = self.head(engine)
        self.finish(engine, successful, result={"recovered": True})
        consumer = receipt["action_ids"][1]
        state = engine.state("set-one", "task-one")
        self.assertEqual(
            state["actions"][consumer]["parameters"],
            {
                "prompt": "Frozen request",
                "source": {"recovered": True},
                "source_id": successful,
            },
        )
        self.assertEqual(state["commands"][receipt["command_id"]]["steps"], original)
        self.assertFalse(state["actions"][consumer]["stale"])
        self.finish(engine, consumer, result={"consumed": True})
        next_step = state["actions"][receipt["action_ids"][2]]
        assert next_step["execution"] is not None
        self.assertEqual(next_step["execution"]["agent"], "codex")
        self.finish(engine, receipt["action_ids"][2], result={})
        command = engine.state("set-one", "task-one")["commands"][receipt["command_id"]]
        self.assertEqual(command["outcome"], "succeeded")
        self.assertEqual(
            len(array_value(object_value(command["result"])["actions"])), 7
        )
        self.assertEqual(
            engine.output("set-one", "task-one", first)["output"], RATE_LIMIT
        )

    def test_delay_blocks_later_steps_but_other_tasks_continue_and_cancel_prevents_dispatch(
        self: Self,
    ) -> None:
        other = self.task_directory.with_name("task-two")
        other.mkdir()
        (other / "design.md").write_text("# Other")
        engine = self.engine()
        receipt = self.submit(engine, chain=True)
        self.fail_attempt(engine, receipt["action_ids"][0])
        pool = engine.state("set-one", "task-one")["action_pool"]
        self.assertEqual(
            [step["action"] for step in pool], ["custom", "consume", "redesign"]
        )
        second = engine.command(
            "set-one",
            "task-two",
            {
                "request_id": "other",
                "action": "custom",
                "parameters": {"prompt": "other"},
            },
        )
        self.assertEqual(
            engine.state("set-one", "task-two")["actions"][
                cast(str, second["action_id"])
            ]["status"],
            "started",
        )
        engine.cancel(
            "set-one",
            "task-one",
            {"request_id": "cancel", "command_id": receipt["command_id"]},
        )
        self.advance_time(engine, 120)
        state = engine.state("set-one", "task-one")
        self.assertEqual(
            state["commands"][receipt["command_id"]]["outcome"], "cancelled"
        )
        self.assertEqual(state["actions"][pool[0]["action_id"]]["outcome"], "cancelled")
        self.assertEqual(state["action_pool"], [])
        self.assertEqual(cast(Mock, engine._runner.initiate).call_count, 2)

    def test_unknown_errors_and_terminal_failures_cancel_pool_without_retry(
        self: Self,
    ) -> None:
        for origin in ("agent", "validation", "launch", "worker", "timed_out"):
            with self.subTest(origin=origin):
                engine = self.engine()
                receipt = engine.command(
                    "set-one",
                    "task-one",
                    {
                        "request_id": origin,
                        "command": "chain",
                        "parameters": {"prompt": "run"},
                    },
                )
                # A retry phrase from earlier output cannot make validation/worker failures retryable.
                self.fail_attempt(
                    engine,
                    receipt["action_ids"][0],
                    "unknown" if origin == "agent" else "rate_limit",
                    origin=origin,
                )
                state = engine.state("set-one", "task-one")
                self.assertEqual(
                    state["commands"][receipt["command_id"]]["outcome"], "failed"
                )
                self.assertEqual(state["action_pool"], [])
                self.assertFalse(
                    any(
                        event["type"] == "retry_scheduled"
                        and event["command_id"] == receipt["command_id"]
                        for event in engine.events("set-one", "task-one")["events"]
                    )
                )
                engine.close()

    def test_all_five_real_handlers_recover_zero_exit_refusal_and_preserve_invocation(
        self: Self,
    ) -> None:
        self.behavior.write_text(json.dumps({"codex": "zero_refusal"}))
        engine = self.engine(workers=True)
        evidence = self.task_directory / "evidence.json"
        evidence.write_text('{"status":"failed"}')
        artifact = self.task_directory / "trial.json"
        artifact.write_text('{"status":"success"}')
        for name in (
            "custom",
            "redesign",
            "impl",
            "local_bench_analyze",
            "troubleshoot",
        ):
            with self.subTest(action=name):
                source: JsonObject = {
                    "design_sha256": digest(self.task_directory / "design.md"),
                    "template_sha256": digest(
                        self.task_directory.parent / "template.md"
                    ),
                    "approved": True,
                }
                values: JsonObject = {"prompt": "Keep {literal} request"}
                if name == "impl":
                    values["source"] = source
                elif name == "local_bench_analyze":
                    values.update(
                        {
                            "source": {
                                **source,
                                "run_completed": True,
                                "trajectories": [str(artifact)],
                            },
                            "source_id": "benchmark-id",
                        }
                    )
                elif name == "troubleshoot":
                    values["evidence_path"] = str(evidence)
                receipt = engine.command(
                    "set-one",
                    "task-one",
                    {"request_id": name, "action": name, "parameters": values},
                )
                self.wait_for(
                    lambda: (
                        engine.state("set-one", "task-one")["commands"][
                            receipt["command_id"]
                        ]["status"]
                        == "completed"
                    )
                )
                state = engine.state("set-one", "task-one")
                command = state["commands"][receipt["command_id"]]
                self.assertEqual(command["outcome"], "succeeded", command["error"])
                self.assertEqual(len(command["action_ids"]), 2)
                failed, replacement = [
                    state["actions"][identity] for identity in command["action_ids"]
                ]
                assert (
                    failed["failure"] is not None
                    and failed["execution"] is not None
                    and replacement["execution"] is not None
                )
                self.assertEqual(failed["failure"]["classification"], "cyber_safety")
                self.assertEqual(failed["failure"]["exit_code"], 0)
                self.assertEqual(replacement["parameters"], values)
                for key in ("guide", "skill", "templates_path", "agent_binaries"):
                    self.assertEqual(
                        failed["execution"].get(key), replacement["execution"].get(key)
                    )
                self.assertNotEqual(
                    action_configuration(engine, ("set-one", "task-one"), failed)[
                        "job_path"
                    ],
                    action_configuration(engine, ("set-one", "task-one"), replacement)[
                        "job_path"
                    ],
                )
                self.assertIn(
                    "Trusted Access",
                    engine.output("set-one", "task-one", failed["action_id"])["output"],
                )
        launches = [
            json.loads(line) for line in self.invocations.read_text().splitlines()
        ]
        self.assertEqual([row["agent"] for row in launches], ["codex", "claude"] * 5)
        for launch in launches[1::2]:
            self.assertIn("claude-opus-4-8", launch["argv"])
            self.assertEqual(launch["disable_1m"], "1")
            self.assertIn("Keep {literal} request", launch["prompt"])

    def test_real_zero_exit_rate_limit_resumes_when_due_and_intermediate_retry_is_success(
        self: Self,
    ) -> None:
        self.behavior.write_text(json.dumps({"codex": "zero_rate"}))
        engine = self.engine(workers=True)
        receipt = self.submit(engine)
        self.wait_for(lambda: engine.state("set-one", "task-one")["action_pool"])
        head = self.head(engine)
        self.assertEqual(
            engine.state("set-one", "task-one")["actions"][head]["status"], "queued"
        )
        self.behavior.write_text(json.dumps({"codex": "intermediate_rate"}))
        # Let the real monitor dispatch the delayed head without an explicit advance.
        self.clock += timedelta(seconds=60)
        self.wait_for(
            lambda: (
                engine.state("set-one", "task-one")["commands"][receipt["command_id"]][
                    "status"
                ]
                == "completed"
            )
        )
        state = engine.state("set-one", "task-one")
        self.assertEqual(
            state["commands"][receipt["command_id"]]["outcome"], "succeeded"
        )
        self.assertIsNone(state["actions"][head]["failure"])
        self.assertEqual(len(state["actions"]), 2)

    def test_restarts_before_and_after_retry_append_and_before_dispatch_are_idempotent(
        self: Self,
    ) -> None:
        for boundary in ("before_retry", "after_retry", "before_start"):
            with self.subTest(boundary=boundary):
                engine = self.engine()
                receipt = engine.command(
                    "set-one",
                    "task-one",
                    {
                        "request_id": boundary,
                        "action": "custom",
                        "parameters": {"prompt": "frozen"},
                    },
                )
                append = engine._store.append

                def crash(key: TaskKey, event: Event) -> None:
                    if event["type"] == (
                        "started" if boundary == "before_start" else "retry_scheduled"
                    ):
                        if boundary == "after_retry":
                            append(key, event)
                        raise OSError("simulated persistence boundary")
                    append(key, event)

                with (
                    patch.object(engine._store, "append", side_effect=crash),
                    self.assertLogs("engine.core", level="ERROR"),
                ):
                    with self.assertRaises(EngineError):
                        self.fail_attempt(
                            engine, cast(str, receipt["action_id"]), "cyber_safety"
                        )
                engine.close()
                restored = self.engine()
                retries = [
                    event
                    for event in restored.events("set-one", "task-one")["events"]
                    if event["type"] == "retry_scheduled"
                    and event["command_id"] == receipt["command_id"]
                ]
                self.assertEqual(len(retries), 1)
                replacement = cast(str, retries[0]["action_id"])
                self.assertEqual(
                    restored.state("set-one", "task-one")["actions"][replacement][
                        "status"
                    ],
                    "started",
                )
                self.finish(restored, replacement, result={"done": True})
                restored.close()

    def test_delayed_pool_rebuild_and_frozen_execution_survive_restart(
        self: Self,
    ) -> None:
        engine = self.engine()
        receipt = self.submit(engine, chain=True)
        self.fail_attempt(engine, receipt["action_ids"][0])
        state = engine.state("set-one", "task-one")
        replacement = state["action_pool"][0]["action_id"]
        engine.close()
        (self.review / "action-pool.json").write_text("bad projection")
        (self.review / "job.json").unlink()
        self.guide.write_text("New guide must not replace captured text")
        with patch.dict("os.environ", {"CODEX_BIN": "changed-binary"}):
            restored = self.engine()
        self.assertEqual(restored.state("set-one", "task-one"), state)
        cast(Mock, restored._runner.initiate).assert_not_called()
        self.advance_time(restored)
        self.assertEqual(
            restored.state("set-one", "task-one")["actions"][replacement]["execution"],
            state["actions"][replacement]["execution"],
        )
        cast(Mock, restored._runner.initiate).assert_called_once()
        self.assertEqual(
            sum(
                event["type"] == "retry_scheduled"
                for event in restored.events("set-one", "task-one")["events"]
            ),
            1,
        )

    def test_removed_parameters_and_cli_option_are_rejected(self: Self) -> None:
        engine = self.engine()
        for body in (
            {
                "request_id": "bad",
                "command": "chain",
                "parameters": {"agent": "codex", "prompt": "x"},
            },
            {
                "request_id": "bad",
                "command": "chain",
                "parameters": {
                    "prompt": "x",
                    "action_parameters": {"custom": {"agent": "codex"}},
                },
            },
            {
                "request_id": "bad",
                "action": "custom",
                "parameters": {"agent": "codex", "prompt": "x"},
            },
            {
                "request_id": "bad",
                "action": "consume",
                "parameters": {"agent": "codex"},
            },
            {
                "request_id": "bad",
                "command": "one-action",
                "parameters": {
                    "action": "custom",
                    "action_parameters": {"agent": "codex", "prompt": "x"},
                },
            },
        ):
            with self.subTest(body=body), self.assertRaises(EngineError):
                engine.command("set-one", "task-one", body)
        for operation, name in (("run", "custom"), ("command", "chain")):
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                cli_main([operation, "set-one", "task-one", name, "--agent", "codex"])
            self.assertEqual(caught.exception.code, 2)
        for definition in engine.list_actions()["actions"].values():
            self.assertNotIn("agent", definition["parameters"])
        self.assertEqual(engine.state("set-one", "task-one")["revision"], 0)

    def test_schema_v2_accepted_invocation_replays_without_automatic_recovery(
        self: Self,
    ) -> None:
        engine = self.engine()
        # Capture a genuinely old plan, including its selected agent parameter.
        with patch.object(engine._scheduler, "advance"):
            receipt = self.submit(engine)
        engine.close()
        log = self.review / "events.jsonl"
        event = json.loads(log.read_text())
        event["schema_version"] = 2
        spec = event["payload"]["steps"][0]["execution"]
        spec.pop("recovery")
        spec.pop("agent_binaries")
        spec["agent"] = "muse"
        event["payload"]["steps"][0]["parameters"]["agent"] = "muse"
        event["payload"]["parameters"]["action_parameters"]["agent"] = "muse"
        event["payload"]["request"]["parameters"]["action_parameters"]["agent"] = "muse"
        event["request_fingerprint"] = fingerprint(
            "run_command", event["payload"]["request"]
        )
        restored = self.engine_from_history(log, event)
        action = restored.state("set-one", "task-one")["actions"][
            cast(str, receipt["action_id"])
        ]
        assert action["execution"] is not None
        self.assertEqual(action["execution"]["agent"], "muse")
        self.assertEqual(action["parameters"]["agent"], "muse")
        self.fail_attempt(restored, action["action_id"], "cyber_safety")
        self.assertEqual(
            restored.state("set-one", "task-one")["commands"][receipt["command_id"]][
                "outcome"
            ],
            "failed",
        )
        self.assertFalse(
            any(
                event["type"] == "retry_scheduled"
                for event in restored.events("set-one", "task-one")["events"]
            )
        )
        restored.close()
        self.engine()

    def test_cyber_refusal_exhausts_muse_immediately_and_cancellation_stops_fallback(
        self: Self,
    ) -> None:
        engine = self.engine()
        receipt = self.submit(engine, chain=True)
        for agent in ("codex", "claude", "muse"):
            identity = self.head(engine)
            spec = engine.state("set-one", "task-one")["actions"][identity]["execution"]
            assert spec is not None
            self.assertEqual(spec["agent"], agent)
            self.fail_attempt(engine, identity, "cyber_safety")
        self.assertEqual(
            engine.state("set-one", "task-one")["commands"][receipt["command_id"]][
                "outcome"
            ],
            "failed",
        )
        self.assertEqual(cast(Mock, engine._runner.initiate).call_count, 3)
        receipt = engine.command(
            "set-one",
            "task-one",
            {
                "request_id": "cancel-running",
                "action": "custom",
                "parameters": {"prompt": "run"},
            },
        )
        engine.cancel(
            "set-one",
            "task-one",
            {"request_id": "cancel", "action_id": receipt["action_id"]},
        )
        self.fail_attempt(engine, cast(str, receipt["action_id"]), "cyber_safety")
        self.assertEqual(
            engine.state("set-one", "task-one")["commands"][receipt["command_id"]][
                "outcome"
            ],
            "cancelled",
        )
        self.assertEqual(cast(Mock, engine._runner.initiate).call_count, 4)

    def test_retry_revalidates_source_and_validation_failure_is_terminal(
        self: Self,
    ) -> None:
        self.behavior.write_text(json.dumps({"codex": "zero_rate"}))
        engine = self.engine(workers=True)
        source: JsonObject = {
            "design_sha256": digest(self.task_directory / "design.md"),
            "template_sha256": digest(self.task_directory.parent / "template.md"),
            "approved": True,
        }
        receipt = engine.command(
            "set-one",
            "task-one",
            {
                "request_id": "source-guard",
                "action": "impl",
                "parameters": {"source": source},
            },
        )
        self.wait_for(lambda: engine.state("set-one", "task-one")["action_pool"])
        replacement = self.head(engine)
        (self.task_directory / "design.md").write_text("# Changed source")
        self.clock += timedelta(seconds=60)
        self.wait_for(
            lambda: (
                engine.state("set-one", "task-one")["commands"][receipt["command_id"]][
                    "status"
                ]
                == "completed"
            )
        )
        state = engine.state("set-one", "task-one")
        failure = state["actions"][replacement]["failure"]
        assert failure is not None
        self.assertEqual(
            (failure["classification"], failure["origin"]), ("unknown", "validation")
        )
        self.assertEqual(state["actions"][replacement]["parameters"]["source"], source)
        self.assertEqual(state["commands"][receipt["command_id"]]["outcome"], "failed")
        self.assertEqual(len(self.invocations.read_text().splitlines()), 1)

    def engine_from_history(self: Self, log: Path, event: JsonObject) -> Engine:
        log.write_text(json.dumps(event) + "\n")
        return self.engine()

    def test_tampered_retry_policy_stops_replay(self: Self) -> None:
        engine = self.engine()
        receipt = self.submit(engine)
        self.fail_attempt(engine, cast(str, receipt["action_id"]))
        engine.close()
        log = self.review / "events.jsonl"
        original = [json.loads(line) for line in log.read_text().splitlines()]
        for field, value in (
            ("attempt_number", 3),
            ("execute_at", self.clock.isoformat().replace("+00:00", "Z")),
            ("model", "muse"),
        ):
            history = copy.deepcopy(original)
            next(event for event in history if event["type"] == "retry_scheduled")[
                "payload"
            ][field] = value
            log.write_text("".join(json.dumps(event) + "\n" for event in history))
            with self.subTest(field=field), self.assertRaises(CorruptLogError):
                self.engine()
