"""Implementation approval action handler."""

from .base import SyncActionHandler
from .common import require_source


class action_commit_impl(SyncActionHandler):
    name = "commit_impl"
    parameters = frozenset({"source"})
    required_parameters = frozenset({"source"})

    def run(self, config, execute, output_path):
        approval = require_source(config, oracle=True)
        return {**approval, "approved": True}
