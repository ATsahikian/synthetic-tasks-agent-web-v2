"""Commit and approve one task's redesigned design document."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Self

from engine.type_defs import ActionConfig, ExecutionCommand, JsonObject

from ..git_worker.transactions import commit_task
from .base import GitActionHandler


class action_commit_desing(GitActionHandler):
    name = "commit_desing"
    parameters = frozenset({"source"})
    required_parameters = frozenset({"source"})

    def run(
        self: Self,
        config: ActionConfig,
        execute: Callable[[ExecutionCommand], int],
        output_path: Path,
    ) -> JsonObject:
        return commit_task(config, execute, design=True)
