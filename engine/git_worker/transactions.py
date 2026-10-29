"""Scoped Git transactions with durable side-effect checkpoints."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Self, Sequence, Unpack, cast

from engine.type_defs import (
    ActionConfig,
    ExecutionCommand,
    GitCheckpoint,
    GitJournal,
    JsonObject,
    PathLike,
)

from ..storage import atomic_json_write


@contextmanager
def lock(file: Path) -> Iterator[None]:
    file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                f"Another Git operation owns {file.name}; retry later."
            ) from error
        yield


def read_git(
    root: Path, *arguments: str, allow_failure: bool = False
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode and not allow_failure:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or "Git check failed."
        )
    return result


def common_directory(root: Path) -> Path:
    return (
        root / read_git(root, "rev-parse", "--git-common-dir").stdout.strip()
    ).resolve()


def file_manifest(root: Path, paths: Sequence[str]) -> dict[str, list[str] | None]:
    manifest: dict[str, list[str] | None] = {}
    for name in paths:
        path = root / name
        if path.is_symlink():
            value, mode = os.fsencode(os.readlink(path)), "120000"
        elif path.is_file():
            value = path.read_bytes()
            mode = "100755" if path.stat().st_mode & 0o111 else "100644"
        elif not path.exists():
            manifest[name] = None
            continue
        else:
            raise RuntimeError(f"Git source must be a file: {name}")
        manifest[name] = [hashlib.sha256(value).hexdigest(), mode]
    return manifest


def tree_manifest(
    root: Path, revision: str, paths: Sequence[str]
) -> dict[str, list[str] | None]:
    if not paths:
        return {}
    specs = [f":(literal){name}" for name in paths]
    output = read_git(root, "ls-tree", "-r", "-z", revision, "--", *specs).stdout
    result: dict[str, list[str] | None] = {name: None for name in paths}
    for row in output.split("\x00"):
        if row:
            metadata, name = row.split("\t", 1)
            mode, _, oid = metadata.split()
            result[name] = [oid, mode]
    return result


class Transaction:
    def __init__(
        self: Self,
        root: Path,
        job: PathLike,
        action_id: str,
        execute: Callable[[ExecutionCommand], int],
    ) -> None:
        self.root, self.job, self.action_id, self.execute = (
            root,
            Path(job),
            action_id,
            execute,
        )
        self.job.mkdir(parents=True, exist_ok=True)
        self.file = self.job / "git-transaction.json"
        if self.file.is_symlink():
            raise RuntimeError("Git transaction journal must not be a symlink.")
        try:
            self.state: GitJournal = (
                cast(GitJournal, json.loads(self.file.read_text("utf-8")))
                if self.file.exists()
                else {}
            )
        except (ValueError, OSError) as error:
            raise RuntimeError(
                "Cannot read Git transaction journal; preserve it for recovery."
            ) from error
        if self.state and (
            self.state.get("repo_path") != str(root)
            or self.state.get("action_id") != action_id
        ):
            raise RuntimeError(
                "Git transaction identity or repository changed; recovery stopped."
            )

    def checkpoint(self: Self, phase: str, **values: Unpack[GitCheckpoint]) -> None:
        self.state.update(
            {
                "schema_version": 1,
                "repo_path": str(self.root),
                "action_id": self.action_id,
                "phase": phase,
                **values,
            }
        )
        atomic_json_write(self.file, self.state)

    def git(self: Self, *arguments: str, timeout_ms: int = 30000) -> None:
        code = self.execute(
            {
                "argv": ["git", *arguments],
                "cwd": str(self.root),
                "literal": True,
                "env": {"GIT_TERMINAL_PROMPT": "0"},
                "timeout_ms": timeout_ms,
            }
        )
        if code:
            output = self.job / "output.log"
            detail = (
                output.read_bytes()[-4096:].decode("utf-8", errors="replace").strip()
                if output.exists()
                else ""
            )
            raise RuntimeError(
                detail or f"git {arguments[0]} failed with exit status {code}."
            )

    def recover_commit(self: Self) -> str | None:
        if "staged" not in self.state:
            return None
        trailer = f"Synthetic-Action-ID: {self.action_id}"
        candidates = read_git(
            self.root,
            "log",
            "--all",
            "--format=%H",
            "--fixed-strings",
            f"--grep={trailer}",
        ).stdout.splitlines()
        for commit in candidates:
            if (
                trailer
                not in read_git(
                    self.root, "show", "-s", "--format=%B", commit
                ).stdout.splitlines()
            ):
                continue
            if (
                tree_manifest(self.root, commit, self.state["paths"])
                != self.state["staged"]
            ):
                raise RuntimeError(
                    "Recovered Git commit does not match the recorded task content."
                )
            if read_git(
                self.root,
                "merge-base",
                "--is-ancestor",
                commit,
                "HEAD",
                allow_failure=True,
            ).returncode:
                continue
            self.checkpoint("committed", commit=commit, committed=True)
            return commit
        if candidates:
            raise RuntimeError(
                "The recorded Git commit is outside the current branch; recovery stopped."
            )
        return None

    def commit(
        self: Self,
        paths: Sequence[str],
        subject: str,
        validate: Callable[[], None] = lambda: None,
    ) -> tuple[str, bool]:
        paths = sorted(paths)
        if self.state.get("paths", paths) != paths:
            raise RuntimeError("Git source paths changed during transaction recovery.")
        recovered = self.recover_commit()
        if recovered:
            return recovered, True
        if self.state.get("phase") == "committed":
            commit = self.state["commit"]
            if tree_manifest(self.root, commit, paths) != self.state["staged"]:
                raise RuntimeError(
                    "Saved Git result no longer matches its recorded content."
                )
            if read_git(
                self.root,
                "merge-base",
                "--is-ancestor",
                commit,
                "HEAD",
                allow_failure=True,
            ).returncode:
                raise RuntimeError(
                    "Saved Git result is outside the current branch; recovery stopped."
                )
            return commit, self.state["committed"]
        validate()
        manifest = file_manifest(self.root, paths)
        if self.state:
            if self.state.get("source", manifest) != manifest:
                raise RuntimeError(
                    "Task files changed during Git transaction recovery; submit a new command."
                )
            if (
                self.state.get("original_head")
                != read_git(self.root, "rev-parse", "HEAD").stdout.strip()
            ):
                raise RuntimeError(
                    "Repository HEAD changed during Git transaction recovery."
                )
        else:
            self.checkpoint(
                "prepared",
                original_head=read_git(self.root, "rev-parse", "HEAD").stdout.strip(),
                paths=paths,
                source=manifest,
            )
        if not paths:
            raise RuntimeError("No task source files are available to commit.")
        specs = [f":(literal){name}" for name in paths]
        self.git("add", "-A", "--", *specs)
        # Save Git's filtered blob IDs, including executable modes and deletions.
        staged: dict[str, list[str] | None] = {name: None for name in paths}
        for row in read_git(
            self.root, "ls-files", "--stage", "-z", "--", *specs
        ).stdout.split("\x00"):
            if row:
                metadata, name = row.split("\t", 1)
                mode, oid, stage = metadata.split()
                if stage != "0":
                    raise RuntimeError("Cannot commit unresolved task files.")
                staged[name] = [oid, mode]
        self.checkpoint("staged", staged=staged)
        validate()
        if file_manifest(self.root, paths) != manifest:
            raise RuntimeError("Task files changed while staging the Git transaction.")
        difference = read_git(
            self.root, "diff", "--cached", "--quiet", "--", *specs, allow_failure=True
        )
        if difference.returncode not in (0, 1):
            raise RuntimeError(
                difference.stderr.strip() or "Cannot inspect staged task changes."
            )
        committed = difference.returncode == 1
        if committed:
            self.checkpoint("commit_pending")
            try:
                self.git(
                    "commit",
                    "--only",
                    "--no-gpg-sign",
                    "-m",
                    subject,
                    "-m",
                    f"Synthetic-Action-ID: {self.action_id}",
                    "--",
                    *specs,
                )
            except Exception:
                if self.recover_commit():
                    return self.state["commit"], True
                raise
        commit = read_git(
            self.root, "log", "-1", "--format=%H", "HEAD", "--", *specs
        ).stdout.strip()
        if not commit:
            raise RuntimeError("No Git commit exists for the selected task files.")
        if tree_manifest(self.root, commit, paths) != staged:
            raise RuntimeError(
                f"Git commit {commit} does not contain the intended task content."
            )
        if committed:
            changed = read_git(
                self.root,
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                commit,
            ).stdout.split("\x00")
            if any(name and name not in paths for name in changed):
                raise RuntimeError(
                    f"Git commit {commit} contains changes outside the selected task files."
                )
        self.checkpoint("committed", commit=commit, committed=committed)
        return commit, committed


LOCAL_OMITTED = {".review", ".remote", ".git", "__pycache__"}


def commit_task(
    config: ActionConfig,
    execute: Callable[[ExecutionCommand], int],
    *,
    design: bool = False,
) -> JsonObject:
    from ..actions.common import design_sources, implementation_digest, require_source

    task = Path(config["task_path"]).resolve()
    if design:
        design_sources(task)
    approval = require_source(config, oracle=not design)
    repository = read_git(task, "rev-parse", "--show-toplevel", allow_failure=True)
    if repository.returncode:
        raise RuntimeError(
            "Commit action requires the task to be inside a Git repository."
        )
    root = Path(repository.stdout.strip()).resolve()
    relative = task.relative_to(root).as_posix()
    transaction = Transaction(
        root, config["job_path"], config["action"]["action_id"], execute
    )

    def implementation_paths() -> list[str]:
        return [
            name
            for name in read_git(
                root,
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                f":(literal){relative}",
            ).stdout.split("\x00")
            if name
            and not (set(Path(name).relative_to(relative).parts) & LOCAL_OMITTED)
            and Path(name).relative_to(relative).as_posix() != "writeup.md"
        ]

    with lock(common_directory(root) / "git-worker.lock"):
        if transaction.state:
            paths = transaction.state["paths"]
        elif design:
            paths = [relative + "/design.md"]
        else:
            paths = implementation_paths()

        def validate() -> None:
            require_source(config, oracle=not design)
            if (
                not design
                and "implementation_sha256" in approval
                and approval["implementation_sha256"] != implementation_digest(task)
            ):
                raise RuntimeError("Implementation sources changed before approval.")
            if not design and set(implementation_paths()) - set(paths):
                raise RuntimeError(
                    "New task files appeared during the Git transaction."
                )

        commit, committed = transaction.commit(
            paths,
            f"Commit {'design' if design else 'implementation'} for {task.parent.name}/{task.name}",
            validate,
        )
        validate()
        if file_manifest(root, paths) != transaction.state["source"]:
            raise RuntimeError(
                "Task files changed after the Git commit; approval was not recorded."
            )
    return {
        **approval,
        "approved": True,
        "repo_path": str(root),
        "commit": commit,
        "committed": committed,
        **(
            {"design_path": relative + "/design.md"}
            if design
            else {
                "task_path": str(task),
                "implementation_sha256": implementation_digest(task),
            }
        ),
    }
