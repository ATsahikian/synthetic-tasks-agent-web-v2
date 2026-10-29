"""Implementation approval action handler."""

from .base import GitActionHandler
from ..git_worker.transactions import commit_task


class action_commit_impl(GitActionHandler):
    name = "commit_impl"
    parameters = frozenset({"source"})
    required_parameters = frozenset({"source"})

    def run(self, config, execute, output_path):
        return commit_task(config, execute)
