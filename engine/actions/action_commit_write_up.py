"""Commit only the validated generated writeup on the durable Git worker."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Self

from engine.type_defs import ActionConfig, ExecutionCommand, JsonObject

from ..git_worker.transactions import Transaction, common_directory, lock, read_git
from .base import GitActionHandler
from .common import digest, read_document, require_implementation


class action_commit_write_up(GitActionHandler):
    name = "commit_write_up"
    parameters = frozenset({"source"})
    required_parameters = frozenset({"source"})

    def run(
        self: Self,
        config: ActionConfig,
        execute: Callable[[ExecutionCommand], int],
        output_path: Path,
    ) -> JsonObject:
        task = Path(config["task_path"]).resolve()
        source = require_implementation(config)
        writeup = task / "writeup.md"

        def validate() -> None:
            require_implementation(config)
            read_document(writeup)
            if source.get("writeup_generated") is not True or source.get(
                "writeup_sha256"
            ) != digest(writeup):
                raise RuntimeError(
                    "Writeup changed or was not generated; rerun write_up before committing."
                )

        validate()
        root = Path(
            read_git(task, "rev-parse", "--show-toplevel").stdout.strip()
        ).resolve()
        if source.get("repo_path") != str(root):
            raise RuntimeError("Implementation source repository changed.")
        transaction = Transaction(
            root, config["job_path"], config["action"]["action_id"], execute
        )
        with lock(common_directory(root) / "git-worker.lock"):
            commit, committed = transaction.commit(
                [writeup.relative_to(root).as_posix()],
                f"Commit writeup for {task.parent.name}/{task.name}",
                validate,
            )
            validate()
        return {**source, "writeup_commit": commit, "writeup_committed": committed}
