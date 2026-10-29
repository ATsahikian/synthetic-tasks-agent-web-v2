"""Commit and approve one task's redesigned design document."""

from .base import GitActionHandler
from ..git_worker.transactions import commit_task


class action_commit_desing(GitActionHandler):
    name = "commit_desing"
    parameters = frozenset({"source"})
    required_parameters = frozenset({"source"})

    def run(self, config, execute, output_path):
        return commit_task(config, execute, design=True)
