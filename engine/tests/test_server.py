import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engine.server import EngineHTTPServer, MAX_BODY_BYTES
from engine.tests.support import EngineFixture


class ServerTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.engine = self.make_engine()
        self.server = EngineHTTPServer(("127.0.0.1", 0), self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.stop_server)

    def stop_server(self):
        port = self.server.server_address[1]
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        # Check that every temporary listener was released.
        with socket.socket() as connection:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            connection.bind(("127.0.0.1", port))

    def request(self, method, path, body=None, *, headers=None):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        request = Request(
            self.url + path,
            data=data, method=method,
            headers=headers or ({"Content-Type": "application/json"} if data is not None else {}),
        )
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            self.assertEqual(response.headers.get_content_type(), "application/json")
            return response.status, json.loads(response.read())

    def test_rest_flow_and_event_pagination(self):
        status, health = self.request("GET", "/health")
        self.assertEqual((status, health["tasks"]), (200, 1))
        status, catalog = self.request("GET", "/api/tasks")
        self.assertEqual(catalog["task_sets"][0]["tasks"][0]["name"], "task-one")
        status, definitions = self.request("GET", "/api/actions")
        self.assertEqual(definitions["actions"]["process"]["dependencies"], ["prepare"])
        task_url = "/api/tasks/set-one/task-one"
        process_body = {"request_id": "process-command", "action": "process"}
        status, process = self.request("POST", task_url + "/commands", process_body)
        self.assertEqual(status, 201)
        status, error = self.request("POST", task_url + "/completions", {
            "request_id": "blocked-completion", "action_id": process["action_id"], "outcome": "succeeded",
        })
        self.assertEqual((status, error["error"]["code"]), (409, "action_not_started"))
        _, prepare = self.request("POST", task_url + "/commands", {
            "request_id": "prepare-command", "action": "prepare", "parameters": {"input": "demo"},
        })
        completion = {
            "request_id": "prepare-complete",
            "action_id": prepare["action_id"],
            "outcome": "succeeded", "result": {"ok": True},
        }
        status, completed = self.request("POST", task_url + "/completions", completion)
        self.assertEqual(status, 200)
        self.assertEqual((status, completed), self.request("POST", task_url + "/completions", completion))
        self.assertEqual(
            (status, completed),
            self.request("POST", task_url + "/completions", {**completion, "type": "completed"}),
        )
        status, state = self.request("GET", task_url + "/state")
        self.assertEqual(state["actions"][process["action_id"]]["status"], "started")
        self.assertEqual(state["revision"], 5)
        status, tail = self.request("GET", task_url + "/events?after=2")
        self.assertEqual(status, 200)
        self.assertEqual([event["sequence"] for event in tail["events"]], [3, 4, 5])
        self.assertEqual([event["type"] for event in tail["events"]], ["started", "completed", "started"])
        self.assertEqual(tail["events"][1]["payload"]["result"], {"ok": True})
        status, empty_tail = self.request("GET", task_url + "/events?after=999")
        self.assertEqual(empty_tail["events"], [])

    def test_concurrent_http_retries_commit_once(self):
        path = "/api/tasks/set-one/task-one/commands"
        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(
                lambda _: self.request("POST", path, {"request_id": "same-http-request", "action": "prepare"}),
                range(24),
            ))
        self.assertTrue(all(response == responses[0] for response in responses))
        self.assertEqual(responses[0][0], 201)
        self.assertEqual(self.engine.state("set-one", "task-one")["revision"], 2)

    def test_http_errors_are_json_and_reject_invalid_reports(self):
        task_url = "/api/tasks/set-one/task-one"
        for method, path, body, expected_status in (
            ("GET", "/missing", None, 404),
            ("GET", "/api/tasks/set-one/missing/state", None, 404),
            ("GET", "/api/tasks/%2E%2E/task-one/state", None, 400),
            ("GET", "/api/tasks/set-one/task%2Fone/state", None, 400),
            ("GET", task_url + "/events?after=-1", None, 400),
            ("GET", task_url + "/events?after=1&after=2", None, 400),
            ("GET", "/api/tasks?unexpected=1", None, 400),
            ("GET", task_url + "/commands", None, 405),
            ("GET", task_url + "/completions", None, 405),
            ("POST", task_url + "/state", {}, 405),
            ("DELETE", task_url + "/events", None, 405),
            ("POST", task_url + "/commands", b"{broken", 400),
            ("POST", task_url + "/commands", [], 400),
            ("POST", task_url + "/commands", {"action": "prepare"}, 400),
            ("POST", task_url + "/commands", b'{"request_id":"nan","action":"prepare","parameters":{"n":NaN}}', 400),
            ("POST", task_url + "/events", {"request_id": "manual-start", "type": "started"}, 405),
            ("POST", task_url + "/completions", {"request_id": "manual-wait", "type": "waiting", "action_id": "x", "outcome": "succeeded"}, 400),
            ("POST", task_url + "/completions", {"request_id": "manual-start", "type": "started", "action_id": "x", "outcome": "succeeded"}, 400),
            ("POST", task_url + "/completions", {"request_id": "bad", "action_id": [], "outcome": "succeeded"}, 400),
            ("POST", task_url + "/commands", {"request_id": "engine:start:reserved", "action": "prepare"}, 400),
        ):
            with self.subTest(method=method, path=path, body=body):
                status, error = self.request(method, path, body)
                self.assertEqual(status, expected_status)
                self.assertIn("error", error)

    def test_content_type_and_size_limits(self):
        path = "/api/tasks/set-one/task-one/commands"
        status, _ = self.request("POST", path, b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.request("POST", path, b" " * (MAX_BODY_BYTES + 1))
        self.assertEqual(status, 413)

    def test_health_reports_unavailable_after_close(self):
        self.engine.close()
        status, error = self.request("GET", "/health")
        self.assertEqual(status, 503)
        self.assertEqual(error["error"]["code"], "engine_unavailable")
