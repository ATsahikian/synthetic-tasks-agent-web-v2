"""Command-line client for an already running engine; never launches a server."""

import argparse
import json
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .actions import reject_constant


def request(base, path, body=None):
    data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
    try:
        with urlopen(
            Request(
                base.rstrip("/") + path,
                data=data,
                headers={"Content-Type": "application/json"} if data is not None else {},
            ),
            timeout=15,
        ) as response:
            return json.loads(response.read())
    except HTTPError as error:
        detail = json.loads(error.read()).get("error", {})
        raise RuntimeError(f"HTTP {error.code}: {detail.get('message', error.reason)}") from error
    except URLError as error:
        raise RuntimeError(f"Cannot reach the engine: {error.reason}") from error


def json_object(value):
    try:
        value = json.loads(value, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return value
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use a JSON object with finite values.") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:4181")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("health", "tasks", "actions"):
        commands.add_parser(name)
    for name in ("state", "events", "output", "run", "cancel", "auto-iterate"):
        sub = commands.add_parser(name)
        sub.add_argument("task_set")
        sub.add_argument("task")
        if name in ("output", "cancel"):
            sub.add_argument("action_id")
        if name == "run":
            sub.add_argument("action")
        if name in ("events", "output"):
            sub.add_argument("--after", type=int, default=0)
        if name in ("run", "cancel", "auto-iterate"):
            sub.add_argument("--request-id", default=None)
        if name in ("run", "auto-iterate"):
            sub.add_argument("--agent", choices=["codex", "claude", "muse"])
            sub.add_argument("--prompt")
            sub.add_argument("--parameters", type=json_object, default={})
            sub.add_argument("--wait", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command in ("health", "tasks", "actions"):
            path = "/health" if args.command == "health" else f"/api/{args.command}"
            result = request(args.url, path)
        else:
            path = f"/api/tasks/{quote(args.task_set, safe='')}/{quote(args.task, safe='')}"
            if args.command in ("state", "events", "output"):
                suffix = (
                    f"/actions/{quote(args.action_id, safe='')}/output"
                    if args.command == "output"
                    else f"/{args.command}"
                )
                if args.command != "state":
                    suffix += f"?after={args.after}"
                result = request(args.url, path + suffix)
            elif args.command == "cancel":
                result = request(
                    args.url,
                    path + "/cancellations",
                    {
                        "request_id": args.request_id or str(uuid.uuid4()),
                        "action_id": args.action_id,
                    },
                )
            else:
                parameters = args.parameters.copy()
                if args.agent:
                    parameters["agent"] = args.agent
                if args.prompt:
                    parameters["prompt"] = args.prompt
                request_id = args.request_id or str(uuid.uuid4())
                if args.command == "auto-iterate":
                    # The configured dependencies determine the workflow order.
                    if len(request_id) > 100:
                        raise RuntimeError(
                            "auto-iterate request IDs must be at most 100 characters."
                        )
                    if set(parameters) - {"agent", "prompt"}:
                        raise RuntimeError("auto-iterate accepts only agent and prompt parameters.")
                    receipts = []
                    for action in (
                        "redesign",
                        "commit_desing",
                        "impl",
                        "commit_impl",
                        "local_bench",
                        "local_bench_analyze",
                        "export_to_codimango",
                    ):
                        body = {
                            "request_id": f"{request_id}:{action}",
                            "action": action,
                            "parameters": (
                                parameters
                                if action in ("redesign", "impl", "local_bench_analyze")
                                else {}
                            ),
                        }
                        receipts.append(request(args.url, path + "/commands", body))
                    result = {"request_id": request_id, "actions": receipts}
                else:
                    result = request(
                        args.url,
                        path + "/commands",
                        {"request_id": request_id, "action": args.action, "parameters": parameters},
                    )
                print(json.dumps(result, indent=2), flush=True)
                if result.get("type") == "completed":
                    return 0 if result["outcome"] == "succeeded" else 1
                if not args.wait:
                    return 0
                pending = {item["action_id"] for item in result.get("actions", [result])}
                while True:
                    state = request(args.url, path + "/state")
                    actions = [state["actions"][action_id] for action_id in pending]
                    failures = [
                        action
                        for action in actions
                        if action["status"] == "completed" and action["outcome"] != "succeeded"
                    ]
                    if failures or all(action["status"] == "completed" for action in actions):
                        for action in actions:
                            if action["status"] == "completed":
                                print(json.dumps({
                                    "task_set": args.task_set,
                                    "task": args.task,
                                    "action_id": action["action_id"],
                                    "type": "completed",
                                    **{key: action[key] for key in ("outcome", "result", "error")},
                                }, indent=2))
                        return 1 if failures else 0
                    time.sleep(0.5)
        print(json.dumps(result, indent=2))
        return 0
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Leaving the client does not silently cancel an async job.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
