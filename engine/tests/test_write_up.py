"""Writeup boundaries, independent guidance, scoped commits, and durable recovery."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from unittest.mock import patch

from engine.actions import prepare_execution, run_action
from engine.actions.common import design_sources, digest
from engine.commands import load_commands
from engine.core import ENGINE_DIRECTORY, Engine
from engine.json_data import object_value
from engine.tests.support import WorkerFixture, git, task_repository
from engine.type_defs import ActionConfig, ExecutionCommand, JsonObject


class WriteupTests(WorkerFixture):
    def setUp(self: Self) -> None:
        super().setUp()
        self.addCleanup(self.clean_workers)
        task_repository(self.root)
        self.config.write_text((ENGINE_DIRECTORY / "actions/actions.json").read_text())
        (self.task_directory / "task.toml").write_text('[task]\nname = "fixture"\n')
        (self.task_directory / "instruction.md").write_text("Produce the answer.\n")
        guide = self.base / "cyber-guide.md"
        guide.write_text("Cyber Bench authoring restrictions fixture")
        binary = self.base / "fake-muse"
        binary.write_text(
            f"#!{sys.executable}\n"
            + r"""
import json, os, sys
from pathlib import Path
prompt = sys.argv[-1]
with Path(os.environ['WRITEUP_LOG']).open('a') as stream:
    stream.write(json.dumps({'argv': sys.argv, 'prompt': prompt, 'agent': os.environ['SYNTHETIC_AGENT']}) + '\n')
mode = Path(os.environ['WRITEUP_BEHAVIOR']).read_text()
if mode == 'rate':
    print('HTTP 429: Too Many Requests')
    sys.exit(1)
if mode == 'refusal':
    print('ERROR: This content was flagged for possible cybersecurity risk.')
    sys.exit(1)
if mode == 'partial':
    (Path.cwd() / 'writeup.md').write_text('Partial unapproved output')
    sys.exit(1)
(Path.cwd() / 'writeup.md').write_text('# Writeup: Fixture\nHuman review is needed.\n')
print('SYNTHETIC_RESULT={"writeup_generated":true}')
"""
        )
        binary.chmod(0o700)
        self.behavior = self.base / "behavior"
        self.behavior.write_text("success")
        self.invocations = self.base / "invocations.jsonl"
        environment = patch.dict(
            "os.environ",
            {
                "CYBER_BENCH_GUIDE_PATH": str(guide),
                "MUSE_BIN": str(binary),
                "CODEX_BIN": "/invalid/codex-must-not-run",
                "CLAUDE_BIN": "/invalid/claude-must-not-run",
                "WRITEUP_LOG": str(self.invocations),
                "WRITEUP_BEHAVIOR": str(self.behavior),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        config = self.action_config(
            "commit_impl",
            {**design_sources(self.task_directory), "oracle_passed": True},
        )
        self.source = run_action(
            config, self.execute_git, Path(config["job_path"]) / "output.log"
        )

    def action_config(self: Self, name: str, source: JsonObject) -> ActionConfig:
        job = self.review / "fixtures" / str(uuid.uuid4())
        job.mkdir(parents=True)
        (job / "output.log").touch()
        parameters: JsonObject = {"source": copy.deepcopy(source)}
        return {
            "schema_version": 2,
            "action": {
                "action_id": job.name,
                "name": name,
                "task_set": "set-one",
                "task": "task-one",
                "parameters": parameters,
            },
            "execution": prepare_execution({"handler": "action_" + name}, parameters),
            "task_path": str(self.task_directory),
            "job_path": str(job),
        }

    def execute_git(self: Self, command: ExecutionCommand) -> int:
        return subprocess.run(
            command["argv"],
            cwd=command.get("cwd"),
            capture_output=True,
            timeout=15,
            check=False,
        ).returncode

    def generate(
        self: Self,
        config: ActionConfig,
        mutation: Callable[[], object] | None = None,
        *,
        output: str = "# Writeup: Fixture\nHuman review is needed.\n",
        marker: bool = True,
    ) -> tuple[JsonObject, str]:
        prompts: list[str] = []

        def execute(command: ExecutionCommand) -> int:
            prompts.append(command["argv"][-1])
            (self.task_directory / "writeup.md").write_text(output)
            if mutation:
                mutation()
            (Path(config["job_path"]) / "output.log").write_text(
                'SYNTHETIC_RESULT={"writeup_generated":true}\n' if marker else "done\n"
            )
            return 0

        result = run_action(config, execute, Path(config["job_path"]) / "output.log")
        return result, prompts[0]

    def test_each_action_selects_only_its_own_local_guide_and_default(
        self: Self,
    ) -> None:
        impl = self.task_directory.parent / "impl-guidance.md"
        writeup = self.task_directory.parent / "writeup-guidance.md"
        impl.write_text(
            "Unique implementation rules\nRead the entire implementation guide."
        )
        writeup.write_text("Unique writeup style\nRead the entire writeup guide.")
        for local in (True, False):
            with self.subTest(action="write_up", local=local):
                if not local:
                    writeup.unlink()
                result, prompt = self.generate(
                    self.action_config("write_up", self.source)
                )
                self.assertNotIn("Unique implementation rules", prompt)
                self.assertIn(
                    "Unique writeup style" if local else "# Task Writeup", prompt
                )
                self.assertNotIn("# Generic task implementation guidance", prompt)
                self.assertTrue(result["writeup_generated"])
        # An invalid writeup guide must have no effect on implementation.
        writeup.symlink_to(self.base / "missing")
        for local in (True, False):
            with self.subTest(action="impl", local=local):
                if not local:
                    impl.unlink()
                config = self.action_config("impl", self.source)
                prompts: list[str] = []

                def execute(
                    command: ExecutionCommand,
                    prompts: list[str] = prompts,
                    config: ActionConfig = config,
                ) -> int:
                    prompts.append(command["input"])
                    (Path(config["job_path"]) / "output.log").write_text(
                        'SYNTHETIC_RESULT={"oracle_passed":true}\n'
                    )
                    return 0

                result = run_action(
                    config, execute, Path(config["job_path"]) / "output.log"
                )
                self.assertTrue(result["oracle_passed"])
                self.assertIn(
                    "Unique implementation rules"
                    if local
                    else "# Generic task implementation guidance",
                    prompts[0],
                )
                self.assertNotIn("# Task Writeup", prompts[0])
        # An invalid implementation guide must have no effect on writeup generation.
        impl.symlink_to(self.base / "missing")
        writeup.unlink()
        self.generate(self.action_config("write_up", self.source))

    def test_invalid_local_guides_fail_before_agent_execution(self: Self) -> None:
        for action, name in (
            ("impl", "impl-guidance.md"),
            ("write_up", "writeup-guidance.md"),
        ):
            guide = self.task_directory.parent / name
            for invalid in ("empty", "symlink", "directory", "unreadable"):
                with self.subTest(action=action, invalid=invalid):
                    if invalid == "symlink":
                        guide.symlink_to(self.base / "missing")
                    elif invalid == "directory":
                        guide.mkdir()
                    else:
                        guide.write_text("" if invalid == "empty" else "Rules")
                    config = self.action_config(action, self.source)
                    calls: list[ExecutionCommand] = []

                    def execute(
                        command: ExecutionCommand,
                        calls: list[ExecutionCommand] = calls,
                    ) -> int:
                        calls.append(command)
                        return 0

                    original = Path.read_text

                    def read(
                        path: Path,
                        *args: object,
                        guide: Path = guide,
                        invalid: str = invalid,
                        original: Callable[..., str] = original,
                        **kwargs: object,
                    ) -> str:
                        if path == guide and invalid == "unreadable":
                            raise PermissionError("unreadable fixture")
                        return original(path, encoding="utf-8")

                    with (
                        patch.object(Path, "read_text", read),
                        self.assertRaisesRegex(RuntimeError, "document"),
                    ):
                        run_action(
                            config, execute, Path(config["job_path"]) / "output.log"
                        )
                    self.assertEqual(calls, [])
                    if invalid == "directory":
                        guide.rmdir()
                    else:
                        guide.unlink()

    def test_stale_sources_and_missing_approval_fail(self: Self) -> None:
        for field, value in (
            ("approved", False),
            ("oracle_passed", False),
            ("commit", None),
            ("implementation_sha256", "stale"),
            ("design_sha256", "stale"),
            ("template_sha256", "stale"),
        ):
            with self.subTest(field=field):
                source = {**self.source, field: value}
                with self.assertRaises(RuntimeError):
                    self.generate(self.action_config("write_up", source))

    def test_generation_rejects_changes_outside_writeup(self: Self) -> None:
        source_file = self.task_directory / "instruction.md"
        original = source_file.read_bytes()
        original_mode = source_file.stat().st_mode
        extra = self.task_directory / "new-file"
        guide = self.task_directory.parent / "writeup-guidance.md"
        guide.write_text("Original writeup guide")
        mutations: list[tuple[str, Callable[[], object]]] = [
            ("edit", lambda: source_file.write_text("Changed")),
            ("delete", source_file.unlink),
            ("add", lambda: extra.write_text("New")),
            ("mode", lambda: source_file.chmod(0o755)),
            ("directory", extra.mkdir),
            ("guide", lambda: guide.write_text("Modified guide")),
            ("symlink", lambda: extra.symlink_to(source_file)),
        ]
        for name, mutation in mutations:
            with (
                self.subTest(mutation=name),
                self.assertRaisesRegex(RuntimeError, "changed"),
            ):
                self.generate(self.action_config("write_up", self.source), mutation)
            if extra.is_symlink() or extra.is_file():
                extra.unlink()
            elif extra.is_dir():
                extra.rmdir()
            source_file.write_bytes(original)
            source_file.chmod(original_mode)
            guide.write_text("Original writeup guide")

    def test_output_must_be_regular_nonempty_and_confirmed(self: Self) -> None:
        config = self.action_config("write_up", self.source)
        for output, marker in (("", True), ("   \n", True), ("Old output", False)):
            with (
                self.subTest(output=output, marker=marker),
                self.assertRaises(RuntimeError),
            ):
                self.generate(config, output=output, marker=marker)
        writeup = self.task_directory / "writeup.md"
        writeup.unlink()
        with self.assertRaises(RuntimeError):
            run_action(
                config, lambda command: 0, Path(config["job_path"]) / "output.log"
            )
        writeup.symlink_to(self.task_directory / "instruction.md")
        with self.assertRaisesRegex(RuntimeError, "regular"):
            self.generate(config)

    def test_impl_preserves_writeup_and_its_selected_guide(self: Self) -> None:
        writeup = self.task_directory / "writeup.md"
        writeup.write_text("Existing writeup")
        guide = self.task_directory.parent / "impl-guidance.md"
        guide.write_text("Implementation guide")
        for target in (writeup, guide):
            config = self.action_config("impl", self.source)

            def execute(command: ExecutionCommand, target: Path = target) -> int:
                target.write_text("Changed")
                return 0

            with self.subTest(target=target), self.assertRaises(RuntimeError):
                run_action(config, execute, Path(config["job_path"]) / "output.log")
            target.write_text(
                "Existing writeup" if target == writeup else "Implementation guide"
            )

    def test_writeup_commits_only_writeup_and_reuses_unchanged_content(
        self: Self,
    ) -> None:
        # A staged writeup is also excluded from commit_impl.
        writeup = self.task_directory / "writeup.md"
        writeup.write_text("Previous writeup")
        git(self.root, "add", "set-one/task-one/writeup.md")
        config = self.action_config("commit_impl", self.source)
        repeated_impl = run_action(
            config, self.execute_git, Path(config["job_path"]) / "output.log"
        )
        self.assertFalse(repeated_impl["committed"])
        self.assertEqual(
            git(self.root, "diff", "--cached", "--name-only"),
            "set-one/task-one/writeup.md",
        )
        self.assertNotIn(
            "writeup.md", git(self.root, "ls-tree", "-r", "--name-only", "HEAD")
        )
        (self.root / "notes.txt").write_text("Unrelated staging")
        git(self.root, "add", "notes.txt")
        result, _ = self.generate(self.action_config("write_up", self.source))
        config = self.action_config("commit_write_up", result)
        committed = run_action(
            config, self.execute_git, Path(config["job_path"]) / "output.log"
        )
        self.assertTrue(committed["writeup_committed"])
        for field in self.source:
            self.assertEqual(committed[field], self.source[field])
        self.assertEqual(
            git(self.root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
            "set-one/task-one/writeup.md",
        )
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "notes.txt")
        count = git(self.root, "rev-list", "--count", "HEAD")
        for repeated in (config, self.action_config("commit_write_up", result)):
            reused = run_action(
                repeated, self.execute_git, Path(repeated["job_path"]) / "output.log"
            )
            self.assertEqual(reused["writeup_commit"], committed["writeup_commit"])
        self.assertFalse(reused["writeup_committed"])
        self.assertEqual(git(self.root, "rev-list", "--count", "HEAD"), count)

    def test_commit_rejects_changed_writeup_before_staging(self: Self) -> None:
        result, _ = self.generate(self.action_config("write_up", self.source))
        config = self.action_config("commit_write_up", result)
        (self.task_directory / "writeup.md").write_text("Edited after generation")
        count = git(self.root, "rev-list", "--count", "HEAD")
        with self.assertRaisesRegex(RuntimeError, "Writeup changed"):
            run_action(
                config, self.execute_git, Path(config["job_path"]) / "output.log"
            )
        self.assertEqual(git(self.root, "rev-list", "--count", "HEAD"), count)
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")

    def test_one_action_launches_only_muse_and_commits_through_git_worker(
        self: Self,
    ) -> None:
        engine = self.make_engine(workers=True)
        receipt = engine.command(
            "set-one",
            "task-one",
            {
                "request_id": "generate",
                "command": "one-action",
                "parameters": {
                    "action": "write_up",
                    "action_parameters": {
                        "source": self.source,
                        "prompt": "Author request",
                    },
                },
            },
        )
        action = self.finished(engine, receipt["action_ids"][0])
        self.assertEqual(action["outcome"], "succeeded", action["error"])
        result = object_value(action["result"])
        self.assertEqual(
            result["writeup_sha256"], digest(self.task_directory / "writeup.md")
        )
        log = json.loads(self.invocations.read_text())
        self.assertEqual(log["agent"], "muse")
        self.assertEqual(log["argv"][1:4], ["exec", "--provider", "meta"])
        self.assertIn("Author request", log["prompt"])
        receipt = engine.command(
            "set-one",
            "task-one",
            {
                "request_id": "commit",
                "command": "one-action",
                "parameters": {
                    "action": "commit_write_up",
                    "action_parameters": {"source": result},
                },
            },
        )
        action = self.finished(engine, receipt["action_ids"][0])
        self.assertEqual(action["outcome"], "succeeded", action["error"])
        self.assertEqual((action["execution"] or {})["executor"], "git-worker")
        self.assertTrue(object_value(action["result"])["writeup_committed"])

    def test_failed_generation_stops_chain_without_committing_partial_output(
        self: Self,
    ) -> None:
        self.behavior.write_text("partial")
        commands = load_commands()
        commands["writeup-chain"] = copy.deepcopy(commands["one-shot"])
        steps = commands["writeup-chain"]["actions"][4:]
        steps[0]["dependencies"] = []
        steps[0]["parameter_bindings"] = {}
        commands["writeup-chain"]["actions"] = steps
        engine = self.make_engine(workers=True, commands=commands)
        head = git(self.root, "rev-parse", "HEAD")
        receipt = engine.command(
            "set-one",
            "task-one",
            {
                "request_id": "fail",
                "command": "writeup-chain",
                "parameters": {
                    "action_parameters": {"write_up": {"source": self.source}}
                },
            },
        )
        self.finished(engine, receipt["action_ids"][0])
        state = engine.state("set-one", "task-one")
        self.assertEqual(state["commands"][receipt["command_id"]]["outcome"], "failed")
        self.assertTrue(
            all(
                state["actions"][identity]["outcome"] == "cancelled"
                for identity in receipt["action_ids"][1:]
            )
        )
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(
            (self.task_directory / "writeup.md").read_text(),
            "Partial unapproved output",
        )

    def test_rate_limit_retries_survive_restart_and_never_switch_from_muse(
        self: Self,
    ) -> None:
        clock = datetime(2026, 10, 29, tzinfo=UTC)
        with patch.object(Engine, "_now", side_effect=lambda: clock):
            self.behavior.write_text("rate")
            engine = self.make_engine(workers=True)
            receipt = self.command(
                engine, "write_up", parameters={"source": self.source}
            )
            self.finished(engine, receipt["action_id"])
            state = engine.state("set-one", "task-one")
            command = state["commands"][receipt["command_id"]]
            retry = command["action_ids"][-1]
            self.assertEqual(
                (state["actions"][retry]["execution"] or {})["agent"], "muse"
            )
            engine.close()
            self.behavior.write_text("success")
            # The accepted binary path must survive changes to the environment.
            with patch.dict(os.environ, {"MUSE_BIN": "/invalid/new-muse"}):
                restored = self.make_engine(workers=True)
                clock += timedelta(seconds=60)
                with restored._lock:
                    restored._start_ready(("set-one", "task-one"))
                action = self.finished(restored, retry)
                self.assertEqual(action["outcome"], "succeeded", action["error"])
            invocations = [
                json.loads(line) for line in self.invocations.read_text().splitlines()
            ]
            self.assertEqual([row["agent"] for row in invocations], ["muse", "muse"])

    def test_writeup_commit_recovers_after_abrupt_process_exit_without_duplicate(
        self: Self,
    ) -> None:
        result, _ = self.generate(self.action_config("write_up", self.source))
        config = self.action_config("commit_write_up", result)
        saved = self.base / "commit-config.json"
        saved.write_text(json.dumps(config))
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                r"""
import json, os, subprocess, sys
from pathlib import Path
from engine.actions import run_action
config = json.loads(Path(sys.argv[1]).read_text())
def execute(command):
    code = subprocess.run(command['argv'], cwd=command['cwd'], capture_output=True).returncode
    if command['argv'][1] == 'commit' and code == 0:
        os._exit(73)
    return code
run_action(config, execute, Path(config['job_path']) / 'output.log')
""",
                str(saved),
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(process.returncode, 73, process.stderr.decode())
        head = git(self.root, "rev-parse", "HEAD")
        recovered = run_action(
            config, self.execute_git, Path(config["job_path"]) / "output.log"
        )
        self.assertEqual(recovered["writeup_commit"], head)
        self.assertTrue(recovered["writeup_committed"])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(
            git(
                self.root,
                "log",
                "--format=%H",
                "--grep",
                "Synthetic-Action-ID: " + config["action"]["action_id"],
            ).splitlines(),
            [head],
        )

    def test_muse_rate_limit_exhaustion_and_refusal_stop_without_fallback(
        self: Self,
    ) -> None:
        clock = datetime(2026, 10, 29, tzinfo=UTC)
        with patch.object(Engine, "_now", side_effect=lambda: clock):
            engine = self.make_engine(workers=True)
            for mode, attempts in (("rate", 3), ("refusal", 1)):
                with self.subTest(mode=mode):
                    self.behavior.write_text(mode)
                    receipt = self.command(
                        engine, "write_up", parameters={"source": self.source}
                    )
                    identity = receipt["action_id"]
                    for number in range(attempts):
                        self.finished(engine, identity)
                        state = engine.state("set-one", "task-one")
                        if number + 1 < attempts:
                            identity = state["commands"][receipt["command_id"]][
                                "action_ids"
                            ][-1]
                            clock += timedelta(seconds=60)
                            with engine._lock:
                                engine._start_ready(("set-one", "task-one"))
                    state = engine.state("set-one", "task-one")
                    command = state["commands"][receipt["command_id"]]
                    self.assertEqual(command["outcome"], "failed")
                    self.assertEqual(len(command["action_ids"]), attempts)
                    self.assertTrue(
                        all(
                            (state["actions"][action]["execution"] or {})["agent"]
                            == "muse"
                            for action in command["action_ids"]
                        )
                    )
            self.assertEqual(len(self.invocations.read_text().splitlines()), 4)

    def test_accepted_six_step_chain_is_preserved_and_new_submissions_use_eight(
        self: Self,
    ) -> None:
        commands = load_commands()
        old = commands["one-shot"]["actions"]
        del old[4:6]
        old[4]["dependencies"] = ["commit_impl"]
        old[4]["parameter_bindings"]["source"]["step"] = "commit_impl"
        engine = self.make_engine(commands=commands)
        body: JsonObject = {
            "request_id": "accepted-before-upgrade",
            "command": "one-shot",
        }
        receipt = engine.command("set-one", "task-one", body)
        frozen = engine.state("set-one", "task-one")["commands"][receipt["command_id"]]
        self.assertEqual(len(frozen["steps"]), 6)
        execution = copy.deepcopy(
            engine.state("set-one", "task-one")["actions"][receipt["action_ids"][0]][
                "execution"
            ]
        )
        engine.close()
        with patch.dict(os.environ, {"CODEX_BIN": "/different/new-codex"}):
            restored = self.make_engine()
            state = restored.state("set-one", "task-one")
            self.assertEqual(
                state["commands"][receipt["command_id"]]["steps"], frozen["steps"]
            )
            self.assertEqual(
                state["actions"][receipt["action_ids"][0]]["execution"], execution
            )
            self.assertEqual(restored.command("set-one", "task-one", body), receipt)
            for identity in receipt["action_ids"]:
                self.finish(restored, identity, result={"fixture": True})
            new = restored.command(
                "set-one",
                "task-one",
                {"request_id": "after-upgrade", "command": "one-shot"},
            )
            state = restored.state("set-one", "task-one")
            self.assertEqual(len(state["commands"][new["command_id"]]["steps"]), 8)
            self.assertEqual(
                (state["actions"][new["action_ids"][4]]["execution"] or {})["agent"],
                "muse",
            )

    def test_git_worker_restart_recovers_writeup_commit_and_emits_one_completion(
        self: Self,
    ) -> None:
        result, _ = self.generate(self.action_config("write_up", self.source))
        source_file = self.base / "generated-source.json"
        source_file.write_text(json.dumps(result))
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                r"""
import json, os, sys, time
from pathlib import Path
from engine.core import Engine
from engine.git_worker.transactions import Transaction
checkpoint = Transaction.checkpoint
def crash(self, phase, **values):
    if phase == 'committed':
        os._exit(88)
    checkpoint(self, phase, **values)
Transaction.checkpoint = crash
engine = Engine(Path(sys.argv[1]), actions_file=Path(sys.argv[2]))
source = json.loads(Path(sys.argv[3]).read_text())
engine.command('set-one', 'task-one', {'request_id':'crashed-writeup', 'command':'one-action', 'parameters':{'action':'commit_write_up', 'action_parameters':{'source':source}}})
time.sleep(15)
os._exit(99)
""",
                str(self.root),
                str(self.config),
                str(source_file),
            ],
            check=False,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(process.returncode, 88, process.stderr.decode())
        head = git(self.root, "rev-parse", "HEAD")
        engine = self.make_engine(workers=True)
        state = engine.state("set-one", "task-one")
        identity = state["current_actions"]["commit_write_up"]
        action = self.finished(engine, identity)
        self.assertEqual(action["outcome"], "succeeded", action["error"])
        self.assertEqual(object_value(action["result"])["writeup_commit"], head)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        completions = [
            event
            for event in engine.events("set-one", "task-one")["events"]
            if event["action_id"] == identity and event["type"] == "completed"
        ]
        self.assertEqual(len(completions), 1)
