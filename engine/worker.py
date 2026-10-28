"""Detached executor: durable input/output/result, one supervised process group."""

import fcntl
import re
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.actions import canonical_json
from engine.storage import atomic_json_write
from engine.workflow import result_marker, run_builtin


def process_identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if stat[0] == "Z" else stat[19]
    except (OSError, IndexError):
        return None


def same_process(pid, identity):
    return (
        type(pid) is int and pid > 0 and identity is not None and process_identity(pid) == identity
    )


def group_exists(pgid):
    return bool(group_members(pgid))


def group_members(pgid):
    members = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdecimal():
            continue
        try:
            stat = Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] != "Z" and int(stat[2]) == pgid:
                members.append(int(entry.name))
        except (OSError, IndexError, ValueError):
            continue
    return members


def owned_group(pgid, action_id):
    if type(pgid) is not int or pgid <= 0:
        return False
    tag = f"SYNTHETIC_JOB_ID={action_id}".encode()
    for pid in group_members(pgid):
        try:
            if tag in Path(f"/proc/{pid}/environ").read_bytes().split(b"\x00"):
                return True
        except OSError:
            continue
    return False


def tagged_groups(action_id):
    groups = set()
    tag = f"SYNTHETIC_JOB_ID={action_id}".encode()
    for entry in os.scandir("/proc"):
        if not entry.name.isdecimal():
            continue
        try:
            if tag not in Path(entry.path, "environ").read_bytes().split(b"\x00"):
                continue
            stat = Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] != "Z":
                groups.add(int(stat[2]))
        except (OSError, IndexError, ValueError):
            continue
    return groups


def stop_group(pgid, grace):
    if not pgid:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def render(value, context):
    # Only known placeholders are expanded; inserted text is never interpreted.
    def replace(match):
        key = match[1]
        if key not in context:
            raise RuntimeError(f"Unknown runner placeholder {{{key}}}.")
        value = context[key]
        return value if isinstance(value, str) else canonical_json(value)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}", replace, value)


class Executor:
    def __init__(self, config, job_path, output):
        self.config, self.job_path, self.output = config, job_path, output
        self.outcome = None
        self.context = {
            "task": config["action"]["task"],
            "task_set": config["action"]["task_set"],
            "task_path": config["task_path"],
            "task_set_path": str(Path(config["task_path"]).parent),
            "job_path": str(job_path),
            "action_id": config["action"]["action_id"],
        }
        self.context.update(
            {f"parameters.{key}": value for key, value in config["action"]["parameters"].items()}
        )
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "SYNTHETIC_TASK": self.context["task"],
                "SYNTHETIC_TASK_SET": self.context["task_set"],
                "SYNTHETIC_TASK_PATH": self.context["task_path"],
                "SYNTHETIC_JOB_ID": self.context["action_id"],
                "SYNTHETIC_WORKSPACE_ROOT": str(Path(config["task_path"]).parent.parent),
                "SYNTHETIC_AGENT": config["execution"].get("agent", "script"),
            }
        )

    def execute(self, command):
        if (self.job_path / "cancel.json").exists():
            self.outcome = "cancelled"
            raise RuntimeError("Action cancelled.")
        expand = (
            (lambda value: value)
            if command.get("literal")
            else (lambda value: render(value, self.context))
        )
        argv = [expand(value) for value in command["argv"]]
        environment = {
            **self.environment,
            **{key: expand(value) for key, value in command.get("env", {}).items()},
        }
        cwd = expand(command.get("cwd", self.config["task_path"]))
        input_text = expand(command["input"]) if "input" in command else None
        # Write input to a file so a non-reading process cannot block timeout/cancel.
        input_file = self.job_path / "stdin.txt"
        input_file.write_text(input_text or "", "utf-8")
        with input_file.open("rb") as stdin:
            child = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=stdin,
                stdout=self.output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        record = {"pid": child.pid, "identity": process_identity(child.pid), "pgid": child.pid}
        atomic_json_write(self.job_path / "process.json", record)
        timeout = command.get("timeout_ms", 3600000)
        deadline = time.monotonic() + timeout / 1000 if timeout is not None else None
        grace = command.get("termination_grace_ms", 10000)
        grace = 10 if grace is None else grace / 1000
        try:
            while child.poll() is None:
                if (self.job_path / "cancel.json").exists():
                    self.outcome = "cancelled"
                    stop_group(child.pid, grace)
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    self.outcome = "timed_out"
                    stop_group(child.pid, grace)
                    break
                time.sleep(0.1)
            code = child.wait()
        finally:
            # Reap the group leader first; otherwise its zombie can consume grace.
            if child.poll() is None:
                stop_group(child.pid, grace)
            child.wait()
            stop_group(child.pid, grace)
            atomic_json_write(self.job_path / "process.json", {})
        self.output.flush()
        os.fsync(self.output.fileno())
        if self.outcome:
            raise RuntimeError(f"Action {self.outcome}.")
        return code


def main(config_path):
    config_path = Path(config_path)
    job_path = config_path.parent
    # A worker owns its durable directory. Repeated launches do not execute twice.
    with (job_path / "worker.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if (job_path / "result.json").exists():
            return 0
        config = json.loads(config_path.read_text("utf-8"))
        atomic_json_write(
            job_path / "worker.json",
            {"pid": os.getpid(), "identity": process_identity(os.getpid())},
        )
        with (job_path / "output.log").open("ab", buffering=0) as output:
            executor = Executor(config, job_path, output)
            try:
                if (job_path / "cancel.json").exists():
                    executor.outcome = "cancelled"
                    raise RuntimeError("Action cancelled.")
                if "builtin" in config["execution"]:
                    result = run_builtin(config, executor.execute, job_path / "output.log")
                else:
                    code = executor.execute(config["execution"])
                    if code != 0:
                        raise RuntimeError(f"Command exited with status {code}.")
                    result = result_marker(job_path / "output.log")
                    expected = config["execution"].get("success_result", {})
                    if any(
                        canonical_json(result.get(key)) != canonical_json(value)
                        for key, value in expected.items()
                    ):
                        raise RuntimeError("Command did not emit its required SYNTHETIC_RESULT.")
                    result = {"exit_code": code, **result}
                completion = {"outcome": "succeeded", "result": result, "error": None}
            except BaseException as error:
                output.write(f"[engine] {error}\n".encode("utf-8", errors="replace"))
                completion = {
                    "outcome": executor.outcome or "failed",
                    "result": None,
                    "error": str(error),
                }
            output.flush()
            os.fsync(output.fileno())
            atomic_json_write(job_path / "result.json", completion)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
