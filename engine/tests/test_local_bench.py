from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Self
from unittest.mock import patch

from engine.actions.common import digest
from engine.actions.metacode_cache import metacode_local_cache
from engine.json_data import object_value, text_value
from engine.tests.support import WorkerFixture
from engine.type_defs import ActionState, JsonObject


class LocalBenchTests(WorkerFixture):
    def setUp(self: Self) -> None:
        super().setUp()
        self.addCleanup(self.clean_workers)
        self.set_actions({"local_bench": {"handler": "action_local_bench"}})
        self.cache_source = self.base / "metacode-source"
        self.cache_source.mkdir()
        for name in ("metacode", "metacode-bin"):
            binary = self.cache_source / name
            binary.write_text(f"fixture {name}\n")
            binary.chmod(0o755)
        self.launches = self.base / "launches"
        self.launches.mkdir()
        codimango = self.base / "fake-codimango"
        codimango.write_text(
            f"#!{sys.executable}\n"
            + r"""
import json, os, sys, time
from pathlib import Path
task = sys.argv[sys.argv.index('-p') + 1]
cache = os.environ.get('HARBOR_METACODE_LOCAL_CACHE')
if sys.argv[sys.argv.index('-a') + 1] == 'metacode':
    if not cache:
        raise SystemExit('Missing private MetaCode cache; shared LATEST pull would race.')
    for name in ('metacode', 'metacode-bin'):
        binary = Path(cache) / name
        assert binary.is_file() and binary.stat().st_size and os.access(binary, os.X_OK)
launches = Path(os.environ['TEST_BENCH_LAUNCHES'])
(launches / (task + '.json')).write_text(json.dumps({'cache':cache, 'argv':sys.argv[1:], 'cwd':os.getcwd()}))
deadline = time.monotonic() + 5
while len(list(launches.glob('*.json'))) < int(os.environ.get('TEST_BENCH_TASKS', '1')):
    if time.monotonic() > deadline:
        raise SystemExit('Sibling benchmarks did not launch concurrently.')
    time.sleep(0.05)
root = Path(sys.argv[sys.argv.index('-o') + 1])
attempts = int(sys.argv[sys.argv.index('-k') + 1])
for index in range(attempts):
    trial = root / ('trial-' + str(index))
    trial.mkdir(parents=True)
    (trial / 'result.json').write_text(json.dumps({'trial_name':trial.name, 'finished_at':'fixture', 'verifier_result':{'rewards':{'reward':1}}, 'exception_info':None}))
print('measured trials finished')
"""
        )
        codimango.chmod(0o700)
        environment = patch.dict(
            os.environ,
            {
                "CODIMANGO_BIN": str(codimango),
                "SYNTHETIC_METACODE_CACHE_SOURCE": str(self.cache_source),
                "TEST_BENCH_LAUNCHES": str(self.launches),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("HARBOR_METACODE_LOCAL_CACHE", None)

    def source(self: Self, task_path: Path) -> JsonObject:
        return {
            "approved": True,
            "oracle_passed": True,
            "design_sha256": digest(task_path / "design.md"),
            "template_sha256": digest(task_path.parent / "template.md"),
        }

    def test_three_tasks_launch_concurrently_with_private_caches(self: Self) -> None:
        tasks = ["task-one", "task-two", "task-three"]
        for task in tasks[1:]:
            directory = self.task_directory.parent / task
            directory.mkdir()
            (directory / "design.md").write_text(f"# {task}\n")
        os.environ["TEST_BENCH_TASKS"] = str(len(tasks))
        original = {
            name: digest(self.cache_source / name)
            for name in ("metacode", "metacode-bin")
        }
        engine = self.make_engine(workers=True)
        action_ids: dict[str, str] = {}
        for task in tasks:
            receipt = engine.command(
                "set-one",
                task,
                {
                    "request_id": f"bench-{task}",
                    "command": "one-action",
                    "parameters": {
                        "action": "local_bench",
                        "action_parameters": {
                            "source": self.source(self.task_directory.parent / task),
                            "attempts": 5,
                            "concurrency": 5,
                        },
                    },
                },
            )
            action_ids[task] = text_value(receipt["action_id"])

        def completed() -> list[ActionState] | None:
            actions = [
                engine.state("set-one", task)["actions"][action_ids[task]]
                for task in tasks
            ]
            return (
                actions
                if all(action["status"] == "completed" for action in actions)
                else None
            )

        actions = self.wait_for(completed, timeout=15)
        for action in actions:
            self.assertEqual(action["outcome"], "succeeded", action["error"])
            self.assertEqual(object_value(action["result"])["completed"], 5)
        caches: set[str] = set()
        result_paths: set[str] = set()
        for task, action in zip(tasks, actions):
            launch = json.loads((self.launches / f"{task}.json").read_text())
            caches.add(launch["cache"])
            self.assertFalse(
                Path(launch["cache"]).exists(),
                "Owned cache must be cleaned after completion",
            )
            argv = launch["argv"]
            self.assertEqual(argv[argv.index('-k') + 1], "5")
            self.assertEqual(argv[argv.index('-n') + 1], "5")
            self.assertEqual(launch["cwd"], str(self.task_directory.parent))
            result_paths.add(
                text_value(object_value(action["result"])["result_path"])
            )
        self.assertEqual(len(caches), 3)
        self.assertEqual(len(result_paths), 3)
        self.assertEqual(
            {name: digest(self.cache_source / name) for name in original}, original
        )

    def test_invalid_explicit_cache_fails_before_codimango_launch(self: Self) -> None:
        os.environ["HARBOR_METACODE_LOCAL_CACHE"] = str(self.base / "missing")
        engine = self.make_engine(workers=True)
        receipt = self.command(
            engine,
            "local_bench",
            parameters={"source": self.source(self.task_directory)},
        )
        action = self.finished(engine, receipt["action_id"])
        self.assertEqual(action["outcome"], "failed")
        self.assertIn("HARBOR_METACODE_LOCAL_CACHE", text_value(action["error"]))
        self.assertEqual(list(self.launches.iterdir()), [])

    def test_other_benchmark_agents_do_not_require_metacode(self: Self) -> None:
        os.environ["HARBOR_METACODE_LOCAL_CACHE"] = str(self.base / "missing")
        engine = self.make_engine(workers=True)
        receipt = self.command(
            engine,
            "local_bench",
            parameters={
                "source": self.source(self.task_directory),
                "benchmark_agent": "oracle",
                "attempts": 1,
            },
        )
        action = self.finished(engine, receipt["action_id"])
        self.assertEqual(action["outcome"], "succeeded", action["error"])


class MetacodeCacheTests(unittest.TestCase):
    def test_owned_cache_is_cleaned_when_execution_raises(self: Self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            for name in ("metacode", "metacode-bin"):
                binary = source / name
                binary.write_text("fixture binary\n")
                binary.chmod(0o755)
            environment = {"SYNTHETIC_METACODE_CACHE_SOURCE": str(source)}
            with self.assertRaisesRegex(RuntimeError, "Cancelled fixture"):
                with metacode_local_cache(environment) as cache:
                    self.assertTrue(cache.is_dir())
                    raise RuntimeError("Cancelled fixture")
            self.assertFalse(cache.exists())
            self.assertTrue(source.is_dir())
            environment = {"HARBOR_METACODE_LOCAL_CACHE": str(source)}
            with metacode_local_cache(environment) as configured:
                self.assertEqual(configured, source)
            self.assertTrue(source.is_dir(), "Caller-provided cache must be preserved")

    def test_cross_device_copy_retains_executable_binaries(self: Self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            for name in ("metacode", "metacode-bin"):
                binary = source / name
                binary.write_text(f"fixture {name}\n")
                binary.chmod(0o755)
            environment = {"SYNTHETIC_METACODE_CACHE_SOURCE": str(source)}
            with patch(
                "engine.actions.metacode_cache.os.link",
                side_effect=OSError(errno.EXDEV, "Cross-device link"),
            ):
                with metacode_local_cache(environment) as cache:
                    for name in ("metacode", "metacode-bin"):
                        staged = cache / name
                        original = source / name
                        self.assertEqual(staged.read_bytes(), original.read_bytes())
                        self.assertTrue(os.access(staged, os.X_OK))
                        self.assertNotEqual(staged.stat().st_ino, original.stat().st_ino)
            self.assertFalse(cache.exists())
