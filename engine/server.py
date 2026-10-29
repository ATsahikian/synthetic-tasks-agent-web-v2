from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self, Sequence, cast
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in (None, ""):
    # Support both "python3 server.py" and "python3 -m engine".
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from engine.actions import reject_constant
from engine.core import DEFAULT_TASKS_ROOT, Engine
from engine.errors import EngineError
from engine.type_defs import JsonValue

LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 1024 * 1024


class EngineHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 64

    def __init__(self: Self, address: tuple[str, int], engine: Engine) -> None:
        self.engine = engine
        super().__init__(address, Handler)

    def get_request(self: Self) -> tuple[socket.socket, tuple[str, int]]:
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server: EngineHTTPServer
    server_version = "TaskEventEngine/0.1"

    def log_message(self: Self, format: str, *args: object) -> None:
        LOGGER.info("%s %s", self.address_string(), format % args)

    def _json_response(self: Self, status: int, value: object) -> None:
        encoded = (
            json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def _read_body(self: Self) -> JsonValue:
        if self.headers.get("Transfer-Encoding"):
            raise EngineError("Transfer-Encoding is unsupported.")
        if (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise EngineError("Content-Type must be application/json.", status=415)
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise EngineError("Content-Length is required.", status=411)
        try:
            length = int(raw_length)
        except ValueError as error:
            raise EngineError("Invalid Content-Length.") from error
        if length <= 0:
            raise EngineError("JSON body is required.")
        if length > MAX_BODY_BYTES:
            raise EngineError("JSON body exceeds 1 MiB.", status=413)
        data = self.rfile.read(length)
        if len(data) != length:
            raise EngineError("Incomplete request body.")
        try:
            return cast(
                JsonValue,
                json.loads(data.decode("utf-8"), parse_constant=reject_constant),
            )
        except (UnicodeError, ValueError, RecursionError) as error:
            raise EngineError(
                "Body must be valid UTF-8 JSON with finite numbers."
            ) from error

    def _route(self: Self) -> tuple[int, object]:
        parsed = urlsplit(self.path)
        parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
        query = parse_qs(parsed.query, keep_blank_values=True)
        engine = self.server.engine
        if self.command == "GET":
            is_events = (
                len(parts) == 5
                and parts[:2] == ["api", "tasks"]
                and parts[4] == "events"
            )
            is_output = (
                len(parts) == 7
                and parts[:2] == ["api", "tasks"]
                and parts[4] == "actions"
                and parts[6] == "output"
            )
            if query and not (is_events or is_output):
                raise EngineError("Unexpected query parameters.")
            if parts == ["health"]:
                return 200, engine.health()
            if parts == ["api", "actions"]:
                return 200, engine.list_actions()
            if parts == ["api", "commands"]:
                return 200, engine.list_commands()
            if parts == ["api", "tasks"]:
                return 200, engine.list_tasks()
            if is_output:
                if set(query) - {"after"} or len(query.get("after", ["0"])) != 1:
                    raise EngineError("Expected at most one after query parameter.")
                after = query.get("after", ["0"])[0]
                if not after.isascii() or not after.isdecimal():
                    raise EngineError("after must be a nonnegative byte offset.")
                return 200, engine.output(
                    parts[2], parts[3], parts[5], after=int(after)
                )
            if len(parts) == 5 and parts[:2] == ["api", "tasks"]:
                task_set, task = parts[2:4]
                if parts[4] == "state":
                    return 200, engine.state(task_set, task)
                if parts[4] == "events":
                    if set(query) - {"after"} or len(query.get("after", ["0"])) != 1:
                        raise EngineError("Expected at most one after query parameter.")
                    after = query.get("after", ["0"])[0]
                    if not after.isascii() or not after.isdecimal():
                        raise EngineError(
                            "after must be a nonnegative integer sequence."
                        )
                    try:
                        after_sequence = int(after)
                    except ValueError as error:
                        raise EngineError("Invalid after sequence.") from error
                    return 200, engine.events(task_set, task, after=after_sequence)
                if parts[4] in ("commands", "cancellations"):
                    raise EngineError("Use POST for this endpoint.", status=405)
        elif self.command == "POST":
            if query:
                raise EngineError("Unexpected query parameters.")
            if len(parts) == 5 and parts[:2] == ["api", "tasks"]:
                if parts[4] == "commands":
                    return 201, engine.command(parts[2], parts[3], self._read_body())
                if parts[4] == "cancellations":
                    return 200, engine.cancel(parts[2], parts[3], self._read_body())
                if parts[4] == "events":
                    raise EngineError(
                        "Events are server-owned and read-only.",
                        status=405,
                    )
                if parts[4] == "state":
                    raise EngineError("Use GET for state.", status=405)
            if parts in (
                ["health"],
                ["api", "tasks"],
                ["api", "actions"],
                ["api", "commands"],
            ):
                raise EngineError("Use GET for this endpoint.", status=405)
        else:
            raise EngineError("Method is unsupported.", status=405)
        raise EngineError("Endpoint not found.", status=404, code="endpoint_not_found")

    def _handle(self: Self) -> None:
        try:
            try:
                status, value = self._route()
            except EngineError as error:
                status = error.status
                value = {"error": {"code": error.code, "message": str(error)}}
            except (socket.timeout, TimeoutError):
                status, value = (
                    408,
                    {
                        "error": {
                            "code": "request_timeout",
                            "message": "Request timed out.",
                        }
                    },
                )
            except Exception:
                LOGGER.exception("Unexpected API error")
                status, value = (
                    500,
                    {
                        "error": {
                            "code": "internal_error",
                            "message": "Internal server error.",
                        }
                    },
                )
            self._json_response(status, value)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            # A lost response is safe to retry with the original request ID.
            self.close_connection = True

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the v2 task event engine.")
    parser.add_argument("--tasks-root", type=Path, default=DEFAULT_TASKS_ROOT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4181)
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    engine = None
    server = None
    try:
        engine = Engine(arguments.tasks_root)
        server = EngineHTTPServer((arguments.host, arguments.port), engine)
        LOGGER.info("Listening on http://%s:%s", *server.server_address[:2])
        LOGGER.info(
            "Task root: %s; discovered tasks: %s",
            arguments.tasks_root.resolve(),
            engine.health()["tasks"],
        )
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            LOGGER.info("Stopping engine")
        return 0
    except (EngineError, OSError, ValueError) as error:
        LOGGER.error("%s", error)
        return 1
    finally:
        if server is not None:
            server.server_close()
        if engine is not None:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
