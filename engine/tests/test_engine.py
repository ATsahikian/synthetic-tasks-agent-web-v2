import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from engine.actions import load_actions
from engine.core import Engine
from engine.errors import CorruptLogError, EngineError
from engine.tests.support import EngineFixture


class EngineTests(EngineFixture):
    def test_discovery_and_no_writes_to_task_documents(self):
        instruction_set = self.root / "instruction-set"
        instruction_task = instruction_set / "instruction-task"
        instruction_task.mkdir(parents=True)
        (instruction_set / "instruction.md").write_text("Set instructions", "utf-8")
        (instruction_task / "instruction.md").write_text("Task instructions", "utf-8")
        toml_task = self.root / "set-one" / "toml-task"
        toml_task.mkdir()
        (toml_task / "task.toml").write_text("[task]\nname = 'toml-task'\n", "utf-8")
        ignored = self.root / "set-one" / "ordinary-folder"
        ignored.mkdir()
        (ignored / "README.md").write_text("Not a task", "utf-8")
        hidden = self.root / "set-one" / ".hidden"
        hidden.mkdir()
        (hidden / "design.md").write_text("Hidden", "utf-8")
        (self.root / "alias-set").symlink_to(self.root / "set-one", target_is_directory=True)
        (self.root / "set-one" / "alias-task").symlink_to(self.task_directory, target_is_directory=True)

        def documents():
            return {
                str(file.relative_to(self.root)): file.read_bytes()
                for file in self.root.rglob("*") if file.is_file() and ".review" not in file.parts
            }

        before = documents()
        engine = self.make_engine()
        catalog = engine.list_tasks()["task_sets"]
        self.assertEqual([entry["name"] for entry in catalog], ["instruction-set", "set-one"])
        self.assertEqual([task["name"] for task in catalog[1]["tasks"]], ["task-one", "toml-task"])
        self.complete_action(engine, "prepare")
        self.assertEqual(documents(), before)
        self.assertFalse((self.task_directory / "job.json").exists())

    def test_empty_root_starts_and_missing_root_is_not_created(self):
        (self.task_directory / "design.md").unlink()
        (self.task_directory.parent / "template.md").unlink()
        engine = self.make_engine()
        self.assertEqual(engine.health()["tasks"], 0)
        self.assertEqual(engine.list_tasks()["task_sets"], [])
        engine.close()
        missing = self.base / "missing"
        with self.assertRaises(EngineError):
            Engine(missing, actions_file=self.config)
        self.assertFalse(missing.exists())

    def test_discovery_refreshes_at_restart(self):
        engine = self.make_engine()
        new_task = self.root / "set-one" / "new-task"
        new_task.mkdir()
        (new_task / "instruction.md").write_text("New", "utf-8")
        self.assertEqual(engine.health()["tasks"], 1)
        engine.close()
        restored = self.make_engine()
        self.assertEqual(restored.health()["tasks"], 2)

    def test_dependency_flow_failure_and_explicit_retry(self):
        engine = self.make_engine()
        process = self.command(engine, "process")["action_id"]
        summarize = self.command(engine, "summarize")["action_id"]
        state = engine.state("set-one", "task-one")
        self.assertEqual(state["actions"][process]["blocked_by"], ["prepare"])
        with self.assertRaises(EngineError) as caught:
            self.report(engine, process, "completed")
        self.assertEqual(caught.exception.code, "action_not_started")
        prepare = self.command(engine, "prepare")["action_id"]
        self.assertEqual(engine.state("set-one", "task-one")["actions"][prepare]["status"], "started")
        self.report(engine, prepare, "completed", result={"prepared": True})
        self.assertEqual(engine.state("set-one", "task-one")["actions"][process]["status"], "started")
        self.report(engine, process, "completed", outcome="failed", error="Demo failure")
        state = engine.state("set-one", "task-one")
        self.assertEqual(state["actions"][summarize]["waiting_reason"], "dependencies")
        self.assertEqual(state["actions"][process]["error"], "Demo failure")
        replacement = self.command(engine, "process")["action_id"]
        self.report(engine, replacement, "completed")
        self.report(engine, summarize, "completed")
        state = engine.state("set-one", "task-one")
        self.assertEqual(state["actions"][summarize]["outcome"], "succeeded")
        self.assertIsNotNone(state["actions"][replacement]["started_at"])
        self.assertEqual(state["actions"][replacement]["dependency_actions"], {"prepare": prepare})
        self.assertEqual(state["actions"][summarize]["dependency_actions"], {"process": replacement})

    def test_duplicate_requests_and_late_results(self):
        engine = self.make_engine()
        original = self.command(engine, "prepare", request_id="command-one")
        self.assertEqual(original, self.command(engine, "prepare", request_id="command-one", parameters={}))
        self.assertEqual(engine.state("set-one", "task-one")["revision"], 2)
        with self.assertRaises(EngineError) as caught:
            self.command(engine, "prepare", request_id="command-one", parameters={"different": True})
        self.assertEqual(caught.exception.code, "request_id_conflict")
        finished = self.report(engine, original["action_id"], "completed", request_id="finish-one")
        self.assertEqual(
            finished,
            self.report(engine, original["action_id"], "completed", request_id="finish-one"),
        )
        replacement = self.command(engine, "prepare")["action_id"]
        with self.assertRaises(EngineError):
            self.report(engine, original["action_id"], "completed", request_id="late-result")
        state = engine.state("set-one", "task-one")
        self.assertEqual(state["current_actions"]["prepare"], replacement)
        self.assertEqual(state["actions"][replacement]["status"], "started")
        with self.assertRaises(EngineError) as caught:
            self.command(engine, "process", request_id="finish-one")
        self.assertEqual(caught.exception.code, "request_id_conflict")

    def test_same_action_type_cannot_overlap_and_clients_cannot_start(self):
        engine = self.make_engine()
        prepare = self.command(engine, "prepare")["action_id"]
        with self.assertRaises(EngineError) as caught:
            self.command(engine, "prepare")
        self.assertEqual(caught.exception.code, "action_already_active")
        with self.assertRaises(EngineError) as caught:
            engine.complete("set-one", "task-one", {
                "request_id": "client-start", "action_id": prepare,
                "type": "started", "outcome": "succeeded",
            })
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("server owns action starts", str(caught.exception))
        self.assertEqual(engine.state("set-one", "task-one")["revision"], 2)

    def test_results_are_preserved_and_staleness_is_transitive(self):
        engine = self.make_engine()
        first_prepare = self.complete_action(engine, "prepare")
        first_process = self.complete_action(engine, "process")
        first_summarize = self.complete_action(engine, "summarize")
        self.assertFalse(engine.state("set-one", "task-one")["actions"][first_summarize]["stale"])
        second_prepare = self.complete_action(engine, "prepare")
        state = engine.state("set-one", "task-one")
        self.assertNotEqual(first_prepare, second_prepare)
        self.assertTrue(state["actions"][first_process]["stale"])
        self.assertTrue(state["actions"][first_summarize]["stale"])
        self.assertEqual(state["actions"][first_process]["outcome"], "succeeded")
        second_summarize = self.command(engine, "summarize")["action_id"]
        self.assertEqual(
            engine.state("set-one", "task-one")["actions"][second_summarize]["waiting_reason"],
            "dependencies",
        )
        second_process = self.complete_action(engine, "process")
        self.report(engine, second_summarize, "completed")
        state = engine.state("set-one", "task-one")
        self.assertEqual(state["actions"][second_summarize]["dependency_actions"], {"process": second_process})
        self.assertFalse(state["actions"][second_summarize]["stale"])
        self.assertEqual(len(state["actions"]), 6)

    def test_running_action_keeps_captured_dependency(self):
        engine = self.make_engine()
        first_prepare = self.complete_action(engine, "prepare")
        process = self.command(engine, "process")["action_id"]
        self.complete_action(engine, "prepare")
        self.report(engine, process, "completed")
        action = engine.state("set-one", "task-one")["actions"][process]
        self.assertEqual(action["dependency_actions"], {"prepare": first_prepare})
        self.assertTrue(action["stale"])

    def test_failed_replacement_preserves_prior_success_but_blocks_new_consumers(self):
        engine = self.make_engine()
        first_prepare = self.complete_action(engine, "prepare")
        first_process = self.complete_action(engine, "process")
        replacement = self.command(engine, "prepare")["action_id"]
        self.report(engine, replacement, "completed", outcome="timed_out")
        next_process = self.command(engine, "process")["action_id"]
        state = engine.state("set-one", "task-one")
        self.assertEqual(state["latest_successful_actions"]["prepare"], first_prepare)
        self.assertFalse(state["actions"][first_process]["stale"])
        self.assertEqual(state["actions"][next_process]["blocked_by"], ["prepare"])

    def test_parallel_mutations_have_unique_contiguous_sequences(self):
        self.set_actions({f"action_{index}": {"dependencies": []} for index in range(12)})
        engine = self.make_engine()
        with ThreadPoolExecutor(max_workers=12) as pool:
            commands = list(pool.map(lambda index: self.command(engine, f"action_{index}"), range(12)))
            completions = list(pool.map(
                lambda acknowledgment: self.report(engine, acknowledgment["action_id"], "completed"),
                commands,
            ))
        self.assertEqual(sorted(item["sequence"] for item in commands), list(range(1, 25, 2)))
        self.assertEqual(sorted(item["sequence"] for item in completions), list(range(25, 37)))
        self.assertEqual(engine.state("set-one", "task-one")["revision"], 36)
        engine.close()
        self.assertEqual(self.make_engine().state("set-one", "task-one")["revision"], 36)

    def test_concurrent_duplicate_commands_commit_once(self):
        engine = self.make_engine()
        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(
                lambda _: self.command(engine, "prepare", request_id="same-command"),
                range(24),
            ))
        self.assertTrue(all(response == responses[0] for response in responses))
        self.assertEqual(engine.state("set-one", "task-one")["revision"], 2)

    def test_start_is_durable_before_initiation_and_retries_do_not_reinitiate(self):
        calls = []

        def initiate(task_set, task, action):
            log = self.root / task_set / task / ".review" / "events.jsonl"
            events = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
            self.assertEqual([event["type"] for event in events], ["waiting", "started"])
            self.assertEqual(events[-1]["action_id"], action["action_id"])
            self.assertEqual(action["status"], "started")
            self.assertEqual(action["parameters"], {"input": 7})
            calls.append(action["action_id"])
            action["parameters"]["input"] = 99

        engine = self.make_engine(initiate_action=initiate)
        original = self.command(engine, "prepare", request_id="initiate-once", parameters={"input": 7})
        self.assertEqual(calls, [original["action_id"]])
        self.assertEqual(
            engine.state("set-one", "task-one")["actions"][original["action_id"]]["parameters"],
            {"input": 7},
        )
        self.assertEqual(
            self.command(engine, "prepare", request_id="initiate-once", parameters={"input": 7}), original,
        )
        engine.close()
        restored = self.make_engine(initiate_action=initiate)
        self.assertEqual(calls, [original["action_id"]])
        self.report(restored, original["action_id"], "completed")

    def test_failed_start_persistence_prevents_initiation_and_restart_resumes(self):
        initiate = mock.Mock()
        engine = self.make_engine(initiate_action=initiate)
        append = engine._store.append

        def fail_start(key, event):
            if event["type"] == "started":
                raise OSError("Start was not persisted")
            append(key, event)

        with mock.patch.object(engine._store, "append", side_effect=fail_start):
            with self.assertLogs("engine.core", level="ERROR"):
                with self.assertRaises(EngineError) as caught:
                    self.command(engine, "prepare", request_id="recover-start")
        self.assertEqual(caught.exception.status, 503)
        initiate.assert_not_called()
        engine.close()
        restored = self.make_engine(initiate_action=initiate)
        acknowledgment = self.command(restored, "prepare", request_id="recover-start")
        self.assertEqual(acknowledgment["sequence"], 1)
        initiate.assert_called_once()
        self.assertEqual(initiate.call_args.args[2]["action_id"], acknowledgment["action_id"])
        self.assertEqual(restored.state("set-one", "task-one")["revision"], 2)

    def test_restart_starts_commands_unblocked_by_durable_completion(self):
        initiate = mock.Mock()
        engine = self.make_engine(initiate_action=initiate)
        process = self.command(engine, "process")["action_id"]
        prepare = self.command(engine, "prepare")["action_id"]
        append = engine._store.append

        def fail_downstream_start(key, event):
            if event["type"] == "started" and event["action_id"] == process:
                raise OSError("Downstream start was not persisted")
            append(key, event)

        with mock.patch.object(engine._store, "append", side_effect=fail_downstream_start):
            with self.assertLogs("engine.core", level="ERROR"):
                with self.assertRaises(EngineError):
                    self.report(engine, prepare, "completed", request_id="durable-completion")
        self.assertEqual(initiate.call_count, 1)
        engine.close()
        restored = self.make_engine(initiate_action=initiate)
        completed = self.report(restored, prepare, "completed", request_id="durable-completion")
        self.assertEqual(completed["sequence"], 4)
        self.assertEqual(initiate.call_count, 2)
        self.assertEqual(initiate.call_args.args[2]["action_id"], process)
        self.assertEqual(restored.state("set-one", "task-one")["actions"][process]["status"], "started")

    def test_initiation_failure_records_failed_completion_and_blocks_dependents(self):
        initiate = mock.Mock(side_effect=OSError("Runner unavailable"))
        engine = self.make_engine(initiate_action=initiate)
        process = self.command(engine, "process")["action_id"]
        with self.assertLogs("engine.core", level="ERROR"):
            prepare = self.command(engine, "prepare", request_id="failed-launch")["action_id"]
        state = engine.state("set-one", "task-one")
        self.assertEqual(state["actions"][prepare]["outcome"], "failed")
        self.assertIn("Runner unavailable", state["actions"][prepare]["error"])
        self.assertEqual(state["actions"][process]["blocked_by"], ["prepare"])
        self.command(engine, "prepare", request_id="failed-launch")
        initiate.assert_called_once()
        engine.close()
        self.assertEqual(self.make_engine(initiate_action=initiate).state("set-one", "task-one"), state)

    def test_initiation_hook_can_signal_synchronous_completion(self):
        calls = []

        def initiate(task_set, task, action):
            calls.append(action["name"])
            engine.complete(task_set, task, {
                "request_id": "sync-" + action["action_id"],
                "action_id": action["action_id"], "outcome": "succeeded",
            })

        engine = self.make_engine(initiate_action=initiate)
        summarize = self.command(engine, "summarize")["action_id"]
        self.command(engine, "process")
        self.command(engine, "prepare")
        self.assertEqual(calls, ["prepare", "process", "summarize"])
        self.assertEqual(engine.state("set-one", "task-one")["actions"][summarize]["outcome"], "succeeded")

    def test_legacy_start_reports_and_direct_completions_remain_replayable(self):
        engine = self.make_engine()
        # Reproduce the original milestone's durable history, before server-owned starts.
        with mock.patch.object(engine, "_start_ready"):
            prepare = self.command(engine, "prepare")["action_id"]
            engine._commit(
                ("set-one", "task-one"),
                {"request_id": "old-finish", "action_id": prepare, "type": "completed",
                 "outcome": "succeeded", "result": None, "error": None},
                "report", "completed", prepare,
                {"dependency_actions": {}, "outcome": "succeeded", "result": None, "error": None},
            )
            process = self.command(engine, "process")["action_id"]
            engine._commit(
                ("set-one", "task-one"),
                {"request_id": "old-start", "action_id": process, "type": "started"},
                "report", "started", process, {"dependency_actions": {"prepare": prepare}},
            )
        state = engine.state("set-one", "task-one")
        engine.close()
        initiate = mock.Mock()
        restored = self.make_engine(initiate_action=initiate)
        self.assertEqual(restored.state("set-one", "task-one"), state)
        initiate.assert_not_called()
        self.report(restored, process, "completed")

    def test_restart_rebuilds_snapshots_and_original_acknowledgments(self):
        engine = self.make_engine()
        original = self.command(engine, "prepare", request_id="persisted-command", parameters={"input": 7})
        self.report(engine, original["action_id"], "completed", result={"value": 9})
        process = self.command(engine, "process")["action_id"]
        state = engine.state("set-one", "task-one")
        engine.close()
        job = self.review / "job.json"
        job.unlink()
        # Configuration changes affect future commands, never historical dependency definitions.
        self.set_actions({"process": {"dependencies": []}})
        restored = self.make_engine()
        self.assertEqual(restored.state("set-one", "task-one"), state)
        self.assertEqual(json.loads(job.read_text("utf-8")), state)
        self.assertEqual(
            self.command(restored, "prepare", request_id="persisted-command", parameters={"input": 7}),
            original,
        )
        restored.close()
        job.write_text("broken snapshot", "utf-8")
        self.assertEqual(self.make_engine().state("set-one", "task-one"), state)

    def test_legacy_invalidation_metadata_is_ignored_during_replay(self):
        engine = self.make_engine()
        self.complete_action(engine, "prepare")
        process = self.complete_action(engine, "process")
        summarize = self.complete_action(engine, "summarize")
        state = engine.state("set-one", "task-one")
        engine.close()

        log = self.review / "events.jsonl"
        events = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
        for event in events:
            if event["type"] == "waiting" and event["action_id"] == process:
                event["payload"]["invalidated_by"] = ["summarize"]
        log.write_text("".join(json.dumps(event) + "\n" for event in events), "utf-8")

        restored = self.make_engine()
        self.assertEqual(restored.state("set-one", "task-one"), state)
        self.complete_action(restored, "prepare")
        updated = restored.state("set-one", "task-one")
        self.assertTrue(updated["actions"][process]["stale"])
        self.assertTrue(updated["actions"][summarize]["stale"])

    def test_task_without_markers_retains_history_in_its_review_folder(self):
        engine = self.make_engine()
        original = self.command(engine, "prepare", request_id="persisted")
        state = engine.state("set-one", "task-one")
        engine.close()
        (self.task_directory / "design.md").unlink()
        restored = self.make_engine()
        self.assertEqual(restored.health()["tasks"], 0)
        self.assertEqual(restored.state("set-one", "task-one"), state)
        self.assertEqual(self.command(restored, "prepare", request_id="persisted"), original)
        with self.assertRaises(EngineError):
            self.command(restored, "process")

    def test_incomplete_trailing_event_is_recovered(self):
        engine = self.make_engine()
        self.complete_action(engine, "prepare")
        state = engine.state("set-one", "task-one")
        engine.close()
        file = self.review / "events.jsonl"
        original = file.read_bytes()
        with file.open("ab") as handle:
            handle.write(b'{"schema_version":1,"sequence":3')
            handle.flush()
            os.fsync(handle.fileno())
        with self.assertLogs("engine.storage", level="WARNING"):
            restored = self.make_engine()
        self.assertEqual(restored.state("set-one", "task-one"), state)
        self.assertEqual(file.read_bytes(), original)

    def test_committed_corruption_stops_startup_and_releases_lock(self):
        engine = self.make_engine()
        self.command(engine, "prepare")
        engine.close()
        file = self.review / "events.jsonl"
        original = file.read_bytes()
        corruptions = [
            b"invalid JSON\n" + original,
            json.dumps({"schema_version": 1}).encode() + b"\n",
        ]
        for content in corruptions:
            with self.subTest(content=content):
                file.write_bytes(content)
                with self.assertRaises(CorruptLogError):
                    self.make_engine()
                with self.assertRaises(CorruptLogError):
                    self.make_engine()
        event = json.loads(original.splitlines()[0])
        event["sequence"] = 9
        file.write_text(json.dumps(event) + "\n", "utf-8")
        with self.assertRaises(CorruptLogError):
            self.make_engine()
        file.write_bytes(original)
        self.assertEqual(self.make_engine().state("set-one", "task-one")["revision"], 2)

    def test_snapshot_failure_does_not_lose_durable_event(self):
        engine = self.make_engine()
        with mock.patch.object(engine._store, "write_snapshot", side_effect=OSError("Snapshot unavailable")):
            with self.assertLogs("engine.core", level="ERROR"):
                original = self.command(engine, "prepare", request_id="snapshot-error")
        self.assertEqual(original["sequence"], 1)
        state = engine.state("set-one", "task-one")
        engine.close()
        restored = self.make_engine()
        self.assertEqual(restored.state("set-one", "task-one"), state)
        self.assertEqual(self.command(restored, "prepare", request_id="snapshot-error"), original)

    def test_uncertain_append_requires_restart_and_retry_deduplicates(self):
        engine = self.make_engine()
        append = engine._store.append

        def append_then_fail(key, event):
            append(key, event)
            raise OSError("Uncertain persistence acknowledgment")

        with mock.patch.object(engine._store, "append", side_effect=append_then_fail):
            with self.assertLogs("engine.core", level="ERROR"):
                with self.assertRaises(EngineError) as caught:
                    self.command(engine, "prepare", request_id="uncertain")
        self.assertEqual(caught.exception.status, 503)
        with self.assertRaises(EngineError):
            engine.health()
        with self.assertRaises(EngineError):
            self.command(engine, "prepare", request_id="uncertain")
        engine.close()
        restored = self.make_engine()
        response = self.command(restored, "prepare", request_id="uncertain")
        self.assertEqual(response["sequence"], 1)
        self.assertEqual(restored.state("set-one", "task-one")["revision"], 2)

    def test_exclusive_task_ownership_and_independent_projects(self):
        engine = self.make_engine()
        with self.assertRaises(EngineError) as caught:
            self.make_engine()
        self.assertEqual(caught.exception.code, "engine_already_running")
        another_root = self.base / "other-tasks"
        another_task = another_root / "set-one" / "task-one"
        another_task.mkdir(parents=True)
        (another_task.parent / "template.md").write_text("# Other set", "utf-8")
        (another_task / "design.md").write_text("# Other task", "utf-8")
        with Engine(another_root, actions_file=self.config) as independent:
            self.assertEqual(independent.health()["tasks"], 1)
            self.assertEqual(independent.state("set-one", "task-one")["revision"], 0)
        engine.close()
        self.assertEqual(self.make_engine().health()["tasks"], 1)

    def test_runtime_is_distributed_among_task_and_task_set_review_folders(self):
        existing_job = self.review / "jobs" / "benchmark" / "old-job" / "result.json"
        existing_job.parent.mkdir(parents=True)
        existing_job.write_text('{"passed": 5}\n', "utf-8")
        second_task = self.task_directory.with_name("task-two")
        second_task.mkdir()
        (second_task / "instruction.md").write_text("# Another task", "utf-8")
        engine = self.make_engine()
        self.complete_action(engine, "prepare")
        self.assertEqual(existing_job.read_text("utf-8"), '{"passed": 5}\n')
        for task_directory in (self.task_directory, second_task):
            review = task_directory / ".review"
            for name in ("events.jsonl", "job.json", "engine.lock"):
                self.assertTrue((review / name).is_file())
            self.assertFalse((review / "workspace.json").exists())
        self.assertEqual(json.loads((self.set_review / "workspace.json").read_text("utf-8")), {
            "schema_version": 1, "tasks_root": str(self.root.resolve()), "task_set": "set-one",
        })
        self.assertEqual(json.loads((self.review / "job.json").read_text("utf-8")), engine.state("set-one", "task-one"))
        self.assertFalse((self.root / ".review").exists())

    def test_project_move_keeps_history_and_refreshes_workspace_metadata(self):
        engine = self.make_engine()
        original = self.command(engine, "prepare", request_id="move-project")
        self.report(engine, original["action_id"], "completed")
        state = engine.state("set-one", "task-one")
        engine.close()
        moved = self.base / "moved-project"
        self.root.rename(moved)
        self.root = moved
        self.task_directory = moved / "set-one" / "task-one"
        self.review = self.task_directory / ".review"
        self.set_review = self.task_directory.parent / ".review"
        restored = self.make_engine()
        self.assertEqual(restored.state("set-one", "task-one"), state)
        self.assertEqual(self.command(restored, "prepare", request_id="move-project"), original)
        manifest = json.loads((self.set_review / "workspace.json").read_text("utf-8"))
        self.assertEqual(manifest["tasks_root"], str(moved.resolve()))

    def test_lock_conflict_releases_other_task_locks_acquired_during_startup(self):
        engine = self.make_engine()
        earlier_task = self.task_directory.with_name("task-a")
        earlier_task.mkdir()
        (earlier_task / "design.md").write_text("# Earlier task", "utf-8")
        with self.assertRaises(EngineError) as caught:
            self.make_engine()
        self.assertEqual(caught.exception.code, "engine_already_running")
        engine.close()
        restored = self.make_engine()
        self.assertEqual(restored.health()["tasks"], 2)

    def test_symlink_review_directory_is_rejected_without_writing_to_target(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.review.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(EngineError):
            self.make_engine()
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_definitions_and_request_validation(self):
        for actions in (
            {"prepare": {"dependencies": ["missing"]}},
            {"prepare": {"dependencies": ["process"]}, "process": {"dependencies": ["prepare"]}},
            {"prepare": {"dependencies": ["process", "process"]}, "process": {"dependencies": []}},
            {"prepare": {"dependencies": [], "invalidated_by": ["process"]},
             "process": {"dependencies": []}},
        ):
            self.set_actions(actions)
            with self.assertRaises(EngineError):
                load_actions(self.config)
        self.set_actions({"prepare": {"dependencies": []}})
        engine = self.make_engine()
        with self.assertRaises(EngineError):
            engine.command("set-one", "task-one", {"request_id": "x", "action": "arbitrary"})
        with self.assertRaises(EngineError):
            engine.command("set-one", "task-one", {"request_id": "x", "action": "prepare", "argv": ["echo"]})
        with self.assertRaises(EngineError):
            self.command(engine, "prepare", parameters={"number": float("nan")})
        with self.assertRaises(EngineError):
            engine.state("..", "task-one")
        with self.assertRaises(EngineError):
            engine.state("set-one", "../task-one")
        for outcome in ("failed", "cancelled", "timed_out"):
            action = self.command(engine, "prepare")["action_id"]
            self.report(engine, action, "completed", outcome=outcome)
            self.assertEqual(engine.state("set-one", "task-one")["actions"][action]["outcome"], outcome)
