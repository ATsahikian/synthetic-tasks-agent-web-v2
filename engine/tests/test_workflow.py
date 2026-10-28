import contextlib
import io
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from engine.cli import main as cli_main
from engine.core import ENGINE_DIRECTORY
from engine.errors import EngineError
from engine.server import EngineHTTPServer
from engine.tests.support import EngineFixture, export_repository, git
from engine.tests import test_runner
from engine.workflow import digest, result_marker, validate_findings


class WorkflowTests(EngineFixture):
    wait_for = test_runner.RunnerTests.wait_for
    finished = test_runner.RunnerTests.finished
    clean_workers = test_runner.RunnerTests.clean_workers

    def setUp(self):
        super().setUp()
        self.addCleanup(self.clean_workers)
        self.config.write_text((ENGINE_DIRECTORY / "actions.json").read_text())
        self.export_root, self.export_remote = export_repository(self.base)
        (self.task_directory / "task.toml").write_text('[task]\nname = "cyber-bench-fixture-1.1-task"\n')
        guide = self.base / "guide.md"
        guide.write_text("Fixture guide preserves literal placeholders such as {task_name}.")
        agent = self.base / "fake-agent"
        agent.write_text(
            f"#!{sys.executable}\n"
            + r"""
import json, re, sys
from pathlib import Path
prompt = sys.stdin.read()
if 'design.candidate.md' in prompt:
    candidate = Path(re.search(r'Write only (.*?/design.candidate.md)\.', prompt)[1])
    candidate.write_text('# Redesigned fixture\n')
elif '# Implement One Task and Pass Its Oracle' in prompt:
    result = 'SYNTHETIC_RESULT={"oracle_passed":true}'
    print(json.dumps({'type':'result','result':result}) if 'stream-json' in sys.argv else result)
elif 'analysis.source.json' in prompt:
    report = Path(re.search(r'Write only (.*?/analysis.md)\.', prompt)[1])
    manifest = json.loads((report.parent / 'analysis.source.json').read_text())
    headings = ['Executive summary','Trajectory source','Benchmark calibration','Trial coverage','Cross-trial findings','Difficulty diagnosis','Weak points','Recommendations','Evidence']
    report.write_text('# Trajectory analysis\n' + '\n'.join('## '+x for x in headings) + '\n' + '\n'.join(manifest['artifact_sha256']))
elif 'troubleshoot.json' in prompt:
    report = Path(re.search(r'Write only (.*?/troubleshoot.json) as', prompt)[1])
    report.write_text(json.dumps({'schema_version':1,'findings':[{'classification':'undetermined','cause':'The checker failed without diagnostic logs.','evidence':[{'pointer':'/status','quote':'failed'}]}]}))
else:
    print('custom fixture done')
"""
        )
        agent.chmod(0o700)
        codimango = self.base / "fake-codimango"
        codimango.write_text(
            f"#!{sys.executable}\n"
            + r"""
import json, sys
from pathlib import Path
root = Path(sys.argv[sys.argv.index('-o')+1])
attempts = int(sys.argv[sys.argv.index('-k')+1])
for index in range(attempts):
    trial = root / ('trial-'+str(index))
    trial.mkdir(parents=True)
    (trial/'result.json').write_text(json.dumps({'trial_name':trial.name,'verifier_result':{'rewards':{'reward':1 if index == 0 else 0}},'exception_info':None}))
    (trial/'trajectory.json').write_text(json.dumps({'steps':[{'role':'assistant','content':'fixture'}]}))
print('measured trials finished')
"""
        )
        codimango.chmod(0o700)
        environment = patch.dict(
            "os.environ",
            {
                "CYBER_BENCH_GUIDE_PATH": str(guide),
                "CODEX_BIN": str(agent),
                "CLAUDE_BIN": str(agent),
                "CODIMANGO_BIN": str(codimango),
                "CODIMANGO_REPO_ROOT": str(self.export_root),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_default_task_workflow_with_oracle_benchmark_and_analysis(self):
        engine = self.make_engine(workers=True)
        redesign = self.command(engine, "redesign", parameters={"prompt": "Revise the fixture"})[
            "action_id"
        ]
        approval = self.command(engine, "commit-desing")["action_id"]
        implement = self.command(engine, "implement", parameters={"agent": "claude"})["action_id"]
        commit = self.command(engine, "commit-impl")["action_id"]
        benchmark = self.command(engine, "local_bench", parameters={"attempts": 2, "concurrency": 2})[
            "action_id"
        ]
        analyze = self.command(engine, "local_bench_analyze", parameters={"agent": "claude"})[
            "action_id"
        ]
        export = self.command(engine, "export_to_codimango")["action_id"]
        self.assertEqual(self.finished(engine, analyze)["outcome"], "succeeded")
        self.assertEqual(self.finished(engine, export)["outcome"], "succeeded")
        state = engine.state("set-one", "task-one")
        self.assertEqual((self.task_directory / "design.md").read_text(), "# Redesigned fixture\n")
        self.assertTrue(state["actions"][implement]["result"]["oracle_passed"])
        self.assertTrue(state["actions"][commit]["result"]["approved"])
        self.assertEqual(state["actions"][commit]["dependency_actions"], {"implement": implement})
        self.assertEqual(state["actions"][benchmark]["dependency_actions"], {"commit-impl": commit})
        self.assertEqual(state["actions"][benchmark]["result"]["passed"], 1)
        self.assertEqual(state["actions"][benchmark]["result"]["total"], 2)
        report = Path(state["actions"][analyze]["result"]["report_path"])
        self.assertTrue(report.exists())
        source = json.loads((report.parent / "analysis.source.json").read_text())
        self.assertEqual(source["benchmark_action_id"], benchmark)
        self.assertEqual(state["actions"][export]["dependency_actions"], {"implement": implement})
        self.assertEqual(
            state["actions"][export]["result"]["push_commit"],
            git(self.export_remote, "rev-parse", "refs/heads/main"),
        )
        self.assertTrue(
            all(
                not state["actions"][action]["stale"]
                for action in (redesign, approval, implement, commit, benchmark, analyze, export)
            )
        )
        self.assertFalse((self.task_directory / "design-status.json").exists())

    def test_changed_design_is_rejected_before_implementation(self):
        engine = self.make_engine(workers=True)
        self.command(engine, "redesign")
        approval = self.command(engine, "commit-desing")["action_id"]
        self.finished(engine, approval)
        (self.task_directory / "design.md").write_text("# Unapproved replacement")
        implement = self.command(engine, "implement")["action_id"]
        result = self.finished(engine, implement)
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("Design or template changed", result["error"])

    def test_export_runs_after_implementation_without_local_benchmark(self):
        engine = self.make_engine(workers=True)
        self.command(engine, "redesign")
        self.command(engine, "commit-desing")
        implement = self.command(engine, "implement")["action_id"]
        export = self.command(engine, "export_to_codimango")["action_id"]
        self.assertEqual(self.finished(engine, implement)["outcome"], "succeeded")
        self.assertEqual(self.finished(engine, export)["outcome"], "succeeded")
        state = engine.state("set-one", "task-one")
        self.assertEqual(state["actions"][export]["dependency_actions"], {"implement": implement})
        self.assertEqual(
            set(state["current_actions"]), {"redesign", "commit-desing", "implement", "export_to_codimango"}
        )

    def test_commit_impl_requires_passing_oracle_and_blocks_benchmark(self):
        definitions = json.loads(self.config.read_text())["actions"]
        implementation = {
            "design_sha256": digest(self.task_directory / "design.md"),
            "template_sha256": digest(self.task_directory.parent / "template.md"),
            "oracle_passed": False,
        }
        self.set_actions(
            {
                "implement": {
                    "dependencies": [],
                    "execution_type": "script",
                    "runner": {
                        "argv": [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
                        "input": "SYNTHETIC_RESULT=" + json.dumps(implementation),
                    },
                },
                "commit-impl": definitions["commit-impl"],
                "local_bench": definitions["local_bench"],
                "export_to_codimango": definitions["export_to_codimango"],
            }
        )
        engine = self.make_engine(workers=True)
        implement = self.command(engine, "implement")["action_id"]
        commit = self.command(engine, "commit-impl")["action_id"]
        benchmark = self.command(engine, "local_bench")["action_id"]
        export = self.command(engine, "export_to_codimango")["action_id"]
        self.assertEqual(self.finished(engine, implement)["outcome"], "succeeded")
        result = self.finished(engine, commit)
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("passing implementation oracle", result["error"])
        self.assertEqual(self.finished(engine, export)["outcome"], "failed")
        self.assertFalse((self.export_root / "cyber-bench-fixture-1.1-task").exists())
        self.assertEqual(
            engine.state("set-one", "task-one")["actions"][benchmark]["blocked_by"],
            ["commit-impl"],
        )

    def test_redesign_allows_changes_to_unrelated_task_files(self):
        from engine.workflow import prepare_execution, run_builtin

        job = self.review / "redesign-fixture"
        job.mkdir(parents=True)
        config = {
            "action": {"parameters": {}},
            "execution": prepare_execution({"builtin": "redesign"}, {}),
            "task_path": str(self.task_directory),
            "job_path": str(job),
        }

        def execute(command):
            # Another action changes its own file while redesign produces its candidate.
            (self.task_directory / "notes.txt").write_text("Unrelated action output")
            (job / "design.candidate.md").write_text("# Revised design")
            return 0

        result = run_builtin(config, execute, job / "output.log")
        self.assertIn("design_sha256", result)
        self.assertEqual((self.task_directory / "design.md").read_text(), "# Revised design")
        self.assertEqual((self.task_directory / "notes.txt").read_text(), "Unrelated action output")

    def test_builtin_parameters_are_rejected_before_events(self):
        engine = self.make_engine(workers=True)
        for name, parameters in [
            ("custom", {}),
            ("custom", {"agent": {}, "prompt": "x"}),
            ("local_bench", {"attempts": False}),
            ("implement", {"argv": ["sh"]}),
            ("commit-impl", {"agent": "codex"}),
            ("export_to_codimango", {"prompt": "push"}),
            ("troubleshoot", {}),
        ]:
            with self.subTest(name=name, parameters=parameters), self.assertRaises(EngineError):
                self.command(engine, name, parameters=parameters)
        self.assertEqual(engine.state("set-one", "task-one")["revision"], 0)

    def test_custom_and_evidence_troubleshooting(self):
        engine = self.make_engine(workers=True)
        custom = self.command(engine, "custom", parameters={"prompt": "Read this task"})[
            "action_id"
        ]
        self.assertEqual(self.finished(engine, custom)["outcome"], "succeeded")
        evidence = self.review / "evidence.json"
        evidence.write_text('{"status":"failed"}')
        troubleshoot = self.command(
            engine, "troubleshoot", parameters={"evidence_path": str(evidence)}
        )["action_id"]
        result = self.finished(engine, troubleshoot)
        self.assertEqual(result["outcome"], "succeeded")
        self.assertTrue(Path(result["result"]["report_path"]).exists())

    def test_tool_output_cannot_supply_an_oracle_marker(self):
        output = self.base / "output.log"
        output.write_text(
            json.dumps({"type": "tool_result", "result": 'SYNTHETIC_RESULT={"oracle_passed":true}'})
            + "\n"
        )
        self.assertEqual(result_marker(output), {})
        output.write_text(
            json.dumps({"type": "result", "result": 'SYNTHETIC_RESULT={"oracle_passed":true}'})
            + "\n"
        )
        self.assertEqual(result_marker(output), {"oracle_passed": True})

    def test_troubleshoot_requires_real_evidence_quotes(self):
        with self.assertRaises(RuntimeError):
            validate_findings(
                {
                    "schema_version": 1,
                    "findings": [
                        {
                            "classification": "not_task_related",
                            "cause": "Invented",
                            "evidence": [{"pointer": "/status", "quote": "passed"}],
                        }
                    ],
                },
                {"status": "failed"},
            )

    def test_cli_runs_pipeline_and_reads_outputs_over_http(self):
        import socket

        engine = self.make_engine(workers=True)
        server = EngineHTTPServer(("127.0.0.1", 0), engine)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        port = server.server_address[1]

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            with socket.socket() as connection:
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                connection.bind(("127.0.0.1", port))

        self.addCleanup(stop)
        base = ["--url", f"http://127.0.0.1:{port}"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                cli_main(
                    base
                    + ["auto-iterate", "set-one", "task-one", "--request-id", "pipeline", "--wait"]
                ),
                0,
            )
            self.assertEqual(
                cli_main(
                    base + ["auto-iterate", "set-one", "task-one", "--request-id", "pipeline"]
                ),
                0,
            )
            action = engine.state("set-one", "task-one")["current_actions"]["implement"]
            self.assertEqual(cli_main(base + ["output", "set-one", "task-one", action]), 0)
            self.assertEqual(cli_main(base + ["events", "set-one", "task-one", "--after", "20"]), 0)
        self.assertIn("oracle_passed", output.getvalue())
        self.assertEqual(engine.state("set-one", "task-one")["revision"], 21)
