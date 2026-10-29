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
    for name in ("health", "tasks", "actions", "commands"):
        commands.add_parser(name)
    for name in ("state", "events", "output", "run", "command", "cancel", "cancel-command"):
        sub = commands.add_parser(name)
        sub.add_argument("task_set")
        sub.add_argument("task")
        if name in ("output", "cancel", "cancel-command"):
            sub.add_argument("command_id" if name == "cancel-command" else "action_id")
        if name in ("run", "command"):
            sub.add_argument("action" if name == "run" else "command_name")
        if name in ("events", "output"):
            sub.add_argument("--after", type=int, default=0)
        if name in ("run", "command", "cancel", "cancel-command"):
            sub.add_argument("--request-id", default=None)
        if name in ("run", "command"):
            sub.add_argument("--agent", choices=["codex", "claude", "muse"])
            sub.add_argument("--prompt")
            sub.add_argument("--parameters", type=json_object, default={})
            sub.add_argument("--wait", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command in ("health", "tasks", "actions", "commands"):
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
            elif args.command in ("cancel", "cancel-command"):
                result = request(
                    args.url,
                    path + "/cancellations",
                    {
                        "request_id": args.request_id or str(uuid.uuid4()),
                        ("action_id" if args.command == "cancel" else "command_id"): (args.action_id if args.command == "cancel" else args.command_id),
                    },
                )
            else:
                parameters = args.parameters.copy()
                if args.agent:
                    parameters["agent"] = args.agent
                if args.prompt:
                    parameters["prompt"] = args.prompt
                request_id = args.request_id or str(uuid.uuid4())
                if args.command == "run":
                    body = {"request_id": request_id, "command": "one-action", "parameters": {
                        "action": args.action, "action_parameters": parameters,
                    }}
                else:
                    body = {"request_id": request_id, "command": args.command_name, "parameters": parameters}
                result = request(args.url, path + "/commands", body)
                print(json.dumps(result, indent=2), flush=True)
                if result.get("type") in ("completed", "command_completed"):
                    return 0 if result["outcome"] == "succeeded" else 1
                if not args.wait:
                    return 0
                pending = set() if result.get("command_id") else {result["action_id"]}
                while True:
                    state = request(args.url, path + "/state")
                    if result.get("command_id"):
                        command = state["commands"][result["command_id"]]
                        if command["status"] == "completed":
                            print(json.dumps({
                                "task_set": args.task_set, "task": args.task,
                                "command_id": command["command_id"], "type": "command_completed",
                                **{key: command[key] for key in ("outcome", "result", "error", "failed_action_id")},
                            }, indent=2))
                            return 0 if command["outcome"] == "succeeded" else 1
                        time.sleep(0.5)
                        continue
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
