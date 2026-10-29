from __future__ import annotations

import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Self, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engine.json_data import loads, object_value
from engine.server import MAX_BODY_BYTES, EngineHTTPServer
from engine.tests.support import WorkerFixture
from engine.type_defs import (
    ActionCatalog,
    Catalog,
    CommandCatalog,
    CommandDefinition,
    EventsResponse,
    JsonObject,
    Receipt,
    State,
)


class ServerTests(WorkerFixture):
    def setUp(self: Self) -> None:
        super().setUp()
        self.addCleanup(self.clean_workers)
        self.gate = self.base / "release"
        self.set_actions(
            {
                "prepare": self.script(
                    f"import time; from pathlib import Path; gate=Path({str(self.gate)!r});\n"
                    "while not gate.exists(): time.sleep(.05)\n"
                    "print('SYNTHETIC_RESULT={\"ok\":true}')"
                ),
                "process": self.script(
                    "print('Processed task')", dependencies=["prepare"]
                ),
                "summarize": self.script(
                    "print('Summarized task')", dependencies=["process"]
                ),
            }
        )
        commands: dict[str, CommandDefinition] = {
            "one-action": {"handler": "command_one_action"},
            "demo": {
                "handler": "command_one_shot",
                "actions": [
                    {
                        "action": name,
                        "dependencies": ([] if index == 0 else [names[index - 1]]),
                        "parameter_bindings": {},
                    }
                    for names in [["prepare", "process", "summarize"]]
                    for index, name in enumerate(names)
                ],
            },
        }
        self.engine = self.make_engine(workers=True, commands=commands)
        self.server = EngineHTTPServer(("127.0.0.1", 0), self.engine)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.stop_server)

    def clean_workers(self: Self) -> None:
        super().clean_workers()
        for child in self.engine._runner.children:
            child.wait(timeout=1)

    def stop_server(self: Self) -> None:
        port = self.server.server_address[1]
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        # Check that every temporary listener was released.
        with socket.socket() as connection:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            connection.bind(("127.0.0.1", port))

    def request(
        self: Self,
        method: str,
        path: str,
        body: object = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, JsonObject]:
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        request = Request(
            self.url + path,
            data=data,
            method=method,
            headers=headers
            or ({"Content-Type": "application/json"} if data is not None else {}),
        )
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            self.assertEqual(response.headers.get_content_type(), "application/json")
            return response.status, object_value(loads(response.read()))

    def test_rest_flow_and_event_pagination(self: Self) -> None:
        status, health = self.request("GET", "/health")
        self.assertEqual((status, health["tasks"]), (200, 1))
        status, catalog_json = self.request("GET", "/api/tasks")
        catalog = cast(Catalog, catalog_json)
        self.assertEqual(catalog["task_sets"][0]["tasks"][0]["name"], "task-one")
        status, definitions_json = self.request("GET", "/api/actions")
        definitions = cast(ActionCatalog, definitions_json)
        self.assertNotIn("dependencies", definitions["actions"]["process"])
        status, commands_json = self.request("GET", "/api/commands")
        commands = cast(CommandCatalog, commands_json)
        self.assertEqual(status, 200)
        self.assertIn("one-action", commands["commands"])
        task_url = "/api/tasks/set-one/task-one"
        body = {
            "request_id": "demo-command",
            "command": "demo",
            "parameters": {"input": "demo"},
        }
        status, receipt_json = self.request("POST", task_url + "/commands", body)
        receipt = cast(Receipt, receipt_json)
        self.assertEqual(status, 201)
        _, state_json = self.request("GET", task_url + "/state")
        state = cast(State, state_json)
        self.assertEqual(
            [
                state["actions"][identity]["status"]
                for identity in receipt["action_ids"]
            ],
            ["started", "queued", "queued"],
        )
        self.gate.touch()
        self.assertEqual(
            self.finished(self.engine, receipt["action_ids"][-1])["outcome"],
            "succeeded",
        )
        status, state_json = self.request("GET", task_url + "/state")
        state = cast(State, state_json)
        self.assertEqual(
            state["actions"][receipt["action_ids"][0]]["result"],
            {"exit_code": 0, "ok": True},
        )
        self.assertEqual(
            state["commands"][receipt["command_id"]]["outcome"], "succeeded"
        )
        self.assertEqual(state["revision"], 8)
        status, tail_json = self.request("GET", task_url + "/events?after=2")
        tail = cast(EventsResponse, tail_json)
        self.assertEqual(status, 200)
        self.assertEqual(
            [event["sequence"] for event in tail["events"]], list(range(3, 9))
        )
        self.assertEqual(
            [event["type"] for event in tail["events"]],
            [
                "completed",
                "started",
                "completed",
                "started",
                "completed",
                "command_completed",
            ],
        )
        self.assertEqual(
            tail["events"][0]["payload"]["result"], {"exit_code": 0, "ok": True}
        )
        _, empty_tail_json = self.request("GET", task_url + "/events?after=999")
        empty_tail = cast(EventsResponse, empty_tail_json)
        self.assertEqual(empty_tail["events"], [])

    def test_concurrent_http_retries_commit_once(self: Self) -> None:
        path = "/api/tasks/set-one/task-one/commands"
        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(
                pool.map(
                    lambda _: self.request(
                        "POST",
                        path,
                        {"request_id": "same-http-request", "action": "prepare"},
                    ),
                    range(24),
                )
            )
        self.assertTrue(all(response == responses[0] for response in responses))
        self.assertEqual(responses[0][0], 201)
        self.assertEqual(self.engine.state("set-one", "task-one")["revision"], 2)

    def test_http_errors_are_json_and_events_are_read_only(self: Self) -> None:
        task_url = "/api/tasks/set-one/task-one"
        for method, path, body, expected_status in (
            ("GET", "/missing", None, 404),
            (
                "POST",
                task_url + "/actions",
                {"request_id": "direct", "action": "prepare"},
                404,
            ),
            (
                "POST",
                task_url + "/commands",
                {
                    "request_id": "runner",
                    "command": "one-action",
                    "parameters": {"action": "prepare", "runner": {}},
                },
                400,
            ),
            (
                "POST",
                task_url + "/commands",
                {
                    "request_id": "inputs",
                    "command": "one-action",
                    "parameters": {"action": "prepare", "inputs": {}},
                },
                400,
            ),
            ("GET", "/api/tasks/set-one/missing/state", None, 404),
            ("GET", "/api/tasks/%2E%2E/task-one/state", None, 400),
            ("GET", "/api/tasks/set-one/task%2Fone/state", None, 400),
            ("GET", task_url + "/events?after=-1", None, 400),
            ("GET", task_url + "/events?after=1&after=2", None, 400),
            ("GET", "/api/tasks?unexpected=1", None, 400),
            ("GET", task_url + "/commands", None, 405),
            ("GET", task_url + "/completions", None, 404),
            ("POST", task_url + "/state", {}, 405),
            ("DELETE", task_url + "/events", None, 405),
            ("POST", task_url + "/commands", b"{broken", 400),
            ("POST", task_url + "/commands", [], 400),
            ("POST", task_url + "/commands", {"action": "prepare"}, 400),
            (
                "POST",
                task_url + "/commands",
                b'{"request_id":"nan","action":"prepare","parameters":{"n":NaN}}',
                400,
            ),
            (
                "POST",
                task_url + "/events",
                {"request_id": "client-start", "type": "started"},
                405,
            ),
            (
                "POST",
                task_url + "/commands",
                {"request_id": "engine:start:reserved", "action": "prepare"},
                400,
            ),
        ):
            with self.subTest(method=method, path=path, body=body):
                status, error = self.request(method, path, body)
                self.assertEqual(status, expected_status)
                self.assertIn("error", error)

    def test_completion_post_cannot_change_worker_state(self: Self) -> None:
        task_url = "/api/tasks/set-one/task-one"
        _, prepare = self.request(
            "POST",
            task_url + "/commands",
            {
                "request_id": "prepare-command",
                "action": "prepare",
            },
        )
        before = self.engine.state("set-one", "task-one")
        status, error = self.request(
            "POST",
            task_url + "/completions",
            {
                "request_id": "external-finish",
                "action_id": prepare["action_id"],
                "outcome": "succeeded",
            },
        )
        self.assertEqual(
            (status, object_value(error["error"])["code"]), (404, "endpoint_not_found")
        )
        self.assertEqual(self.engine.state("set-one", "task-one"), before)

    def test_content_type_and_size_limits(self: Self) -> None:
        path = "/api/tasks/set-one/task-one/commands"
        status, _ = self.request(
            "POST", path, b"{}", headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 415)
        status, _ = self.request("POST", path, b" " * (MAX_BODY_BYTES + 1))
        self.assertEqual(status, 413)

    def test_health_reports_unavailable_after_close(self: Self) -> None:
        self.engine.close()
        status, error = self.request("GET", "/health")
        self.assertEqual(status, 503)
        self.assertEqual(object_value(error["error"])["code"], "engine_unavailable")
